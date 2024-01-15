
import ctypes
import glob
import pandas as pd
import numpy as np
from functools import partial
from tqdm import tqdm
import os
from itertools import chain, combinations
from shap.maskers import Independent
from shap.explainers import Tree
from graphviz import Digraph
from copy import deepcopy

from sklearn.linear_model import LinearRegression, Ridge, LogisticRegression, PoissonRegressor
from sklearn.pipeline import Pipeline

from .plots import color_dict
from .utils import check_Imap_inv, get_Imap_inv_from_pipeline, get_leaf_box, ravel, powerset

SKLEARN_LINEAR = [LinearRegression, Ridge, LogisticRegression, PoissonRegressor]


def get_components_linear(h, foreground, background, Imap_inv=None):
    # Setup
    Imap_inv, D, _ = check_Imap_inv(Imap_inv, background.shape[1])
    if D > 1:
        assert foreground.shape[1] > 1, "When computing several h components, foreground must be a dataset"
    else:
        # If a line is provided we can compute the PDP
        if foreground.ndim == 1:
            foreground = foreground.reshape((-1, 1))

    # Check model type, identify the ML task, and get average prediction
    is_pipeline = False
    if type(h) == Pipeline:
        model_type = type(h.steps[-1][1])
        is_pipeline = True
    else:
        model_type = type(h)
    assert model_type in SKLEARN_LINEAR, "The predictor must be a linear/additive model"
    # For regression we explain the output
    if model_type in [LinearRegression, Ridge, PoissonRegressor]:
        h_emptyset_z = h.predict(background)
    # FOr classification we explain the logit
    else:
        h_emptyset_z = h.decision_function(background)
    decomposition = {}
    decomposition[()] = h_emptyset_z.mean()

    #If h is a Pipeline whose last layer is a linear model, we propagate the foreground and background
    # up to that point and we compute the Imap_inv up to the linear layer
    if is_pipeline:
        preprocessing = h[:-1]
        predictor = h[-1]
        background = preprocessing.transform(background)
        foreground = preprocessing.transform(foreground)
        Imap_inv = get_Imap_inv_from_pipeline(Imap_inv, preprocessing)
    else:
        predictor = h

    w = predictor.coef_.ravel()

    # Compute the additive components
    for j in range(D):
        Imap_inv_j = np.array(Imap_inv[j])
        # TODO this breaks if the processed data is sparse
        decomposition[(j,)] = np.sum((foreground[:, Imap_inv_j] - background[:, Imap_inv_j].mean(0)) * w[Imap_inv_j], axis=1)
    
    return decomposition


#######################################################################################
#                                    Model-Agnostic
#######################################################################################


def setup(foreground, background, Imap_inv, interactions, show_bar):
    d = background.shape[1]
    if type(interactions) == int:
        assert interactions >= 1, "interactions must be 1, 2, 3, ..."
        assert interactions <= d, "interactions cannot be greater than the number of features"
        def iterator_():
            for cardinality in range(1, interactions+1):
                for key in tqdm(combinations(range(D), r=cardinality), desc="Functional Decomposition", disable=not show_bar):
                    yield key
    elif type(interactions) == list:
        assert type(interactions[0]) == tuple, "interactions should be a list of tuples"
        interactions = sorted(interactions, key=len)
        for i in range(len(interactions)):
            interactions[i] = tuple(interactions[i])
            def iterator_():
                for key in tqdm(interactions, desc="Functional Decomposition", disable=not show_bar):
                    yield key
    else:
        raise Exception("interactions must either be an integer or a list of tuples")
    
    # Setup Imap_inv
    Imap_inv, D, _ = check_Imap_inv(Imap_inv, d)

    # The foreground need not have the same shape as the background when one is doing a simple PDP
    one_group = False
    if foreground.ndim == 1:
        foreground = foreground.reshape((-1, 1))
    if D > 1:
        assert foreground.shape[1] == d, "When computing several h components, foreground must be a (Nf, d) dataset"
    elif interactions > 1:
        assert foreground.shape[1] == d, "When computing interactions, foreground must be a (Nf, d) dataset"
    else:
        # The user is computing a PDP
        if foreground.shape[1] == len(Imap_inv[0]):
            one_group = True
        else:
            assert foreground.shape[1] == d, "When computing PDP, foreground must be a (Nf, d) or (Nf, group_size) dataset "
    
    return foreground, Imap_inv, iterator_, one_group



def get_components_brute_force(h, foreground, background, Imap_inv=None, interactions=1, anchored=True, show_bar=True):
    """
    Compute the Anchored/Interventional Decomposition of any black box

    Parameters
    ----------
    h : model X -> R
    foreground : (Nf, d) `np.array`
        The data points at which to evaluate the decomposition
    background : (Nb, d) `np.array`
        The data points at which to anchor the decomposition
    Imap_inv : List(List(int))
        A list of groups that represent a single feature. For instance `[[0, 1], [2]]` will treat
        the columns 1 and 2 as a single feature.
    interactions : int, List(Tuple(int)), default=1
        The highest level of interactions to consider. If it is a list of tuples, then we compute all
        specified U components.
    anchored : bool, default=True
        Flag to compute anchored decompositions or interventional decompositions. If anchored, a component
        is (Nf, Nb) and if interventional it is (Nf, 2) with final index indicating expectancy and std.
    
    Returns
    -------
    decomposition : `dict{Tuple: np.array}`
        The various components of the decomposition indexed via their feature subset e.g. `decomposition[(1, 2, 3)]`
    """
    
    # Setup
    foreground, Imap_inv, iterator_, one_group = setup(foreground, background, Imap_inv, interactions, show_bar)
    N_ref = background.shape[0]
    N_eval = foreground.shape[0]

    # Average prediction
    decomposition = {}

    # Compute all additive components  h_{i, z}(x) = h(x_i, z_-i) - h(z)
    # for all feature groups i in Imap_inv and points x,z
    h_emptyset_z = h(background)
    decomposition[()] = h_emptyset_z

    data_temp = np.copy(background)
    # Model-Agnostic Implementation (very costly in time and memory)
    # Compute h_{u, z}(x) = h(x_{u}, z_-{u}) - sum_{v\subset u} h_{u, z}(x)
    for key in iterator_():
        decomposition[key] = np.zeros((N_eval, N_ref))
        idx = ravel([Imap_inv[i] for i in key])
        for n in range(N_eval):
            if one_group:
                data_temp[:, idx] = foreground[n]
            else:
                data_temp[:, idx] = foreground[n, idx]
            h_ij_z = h(data_temp)
            decomposition[key][n] += h_ij_z
        # Reset the copied background
        data_temp[:,  idx] = background[:,  idx] 
        # Remove all contributions of subsets to get the interaction
        for subset in powerset(key):
            if subset not in decomposition:
                raise Exception("The provided interaction set is not closed downward")
            decomposition[key] -= decomposition[subset]
    if not anchored:
        for key, component in decomposition.items():
            if len(key) >= 1:
                decomposition[key] = np.column_stack((component.mean(1), component.std(1)))
            else:
                decomposition[key] = np.stack((component.mean(), component.std()))
    return decomposition



def get_components_adaptive(h, background, Imap_inv=None, show_bar=True, tolerance=0.05, precompute=None):
    
    # Setup
    Imap_inv, D, is_fullpartition = check_Imap_inv(Imap_inv, background.shape[1])
    assert is_fullpartition, "In adaptive, Imap_inv must be a partition of the input columns"
    N = background.shape[0]
    data_temp = np.copy(background)

    # We compute the additive decomposition if it is not precomputed
    if precompute is None:
        # Average prediction
        decomposition = {}
        decomposition[()] = h(background)

        # Compute all additive components  h_{i, z}(x) = h(x_i, z_-i) - h(z)
        # for all feature groups i in Imap_inv and points x,z
        for i in tqdm(range(D), desc="Additive Components", disable=not show_bar):
            decomposition[(i,)] = np.zeros((N, N))
            for n in range(N):
                data_temp[:, Imap_inv[i]] = background[n, Imap_inv[i]]
                decomposition[(i,)][n] = h(data_temp) - decomposition[()]
            # Reset the copied background
            data_temp[:,  Imap_inv[i]] = background[:,  Imap_inv[i]]

    else:
        assert () in precompute.keys()
        assert precompute[()].shape == (N,)
        for i in range(D):
            assert (i,) in precompute.keys()
            assert precompute[(i,)].shape == (N, N)
        decomposition = deepcopy(precompute)
        
    
    # Setup for the adaptive interaction search
    variance = decomposition[()].var()
    h_proj = get_h_add(decomposition, anchored=True).mean(1)
    loss = np.mean( ( decomposition[()] - h_proj ) ** 2)
    # We get the interaction strenght for each of the current subsets u\in U
    psi, U = get_H_interaction(decomposition)
    # Data structure counting how many times we saw each super set
    seen_super_sets = {}
    # Store the decomposition without necessarily adding it to the dict
    temp_h_replace = np.zeros((N, N))

    # If specified, we compute all interactions up to a given size
    while loss / variance > tolerance:
        choice = np.argmax(psi)
        u = U[choice]
        
        # Never pick this point again
        psi[choice] = -1

        # Update the collection of supersets
        combine_candidates = [(i,) for i in range(D) if not i in u]
        for u_combine in combine_candidates:
            super_set = tuple(sorted(u_combine + u))
            # If never seen we add it to the dict
            if not super_set in seen_super_sets.keys():
                seen_super_sets[super_set] = 1
            # If already seen we increase its counter by one
            else:
                seen_super_sets[super_set] += 1
                # When all of its subsets are in U we can add it to U
                if seen_super_sets[super_set] == len(super_set):
                    
                    # Compute the component of the new added set
                    joint_idx = ravel([Imap_inv[i] for i in super_set])
                    for n in range(N):
                        data_temp[:, joint_idx] = background[n, joint_idx]
                        h_ij_z = h(data_temp)
                        temp_h_replace[n] = h_ij_z
                    
                    # Reset the copied background
                    data_temp[:,  joint_idx] = background[:,  joint_idx] 
                    
                    # Remove all contributions of subsets to get the interaction
                    for subset in powerset(super_set):
                        temp_h_replace -= decomposition[subset]
                    
                    # The component is essentially null so we do not add it
                    if np.mean(temp_h_replace.mean(1)**2) > 1e-8 * variance:
                        U.append(super_set)
                        decomposition[super_set] = np.copy(temp_h_replace)

                        # Update the loss
                        h_proj += decomposition[super_set].mean(1)
                        loss = np.mean( ( decomposition[()] - h_proj ) ** 2)
                        
                        if loss / variance <= tolerance:
                            break
                        
                        # Compute the psi to decide if we must explore the supersets
                        # of the newly added set
                        psi_ = (-1)**len(super_set) * decomposition[super_set].mean(0)
                        psi_ -= decomposition[super_set].mean(1)
                        psi = np.append(psi, np.mean(psi_**2))
    return decomposition



def permutation_shap(h, foreground, background, Imap_inv=None, M=20, show_bar=True, reversed=True, return_nu_evals=False):
    """
    Approximate the Shapley Values of any black box by sampling M permutations

    Parameters
    ----------
    h : model X -> R
    foreground : (Nf, d) `np.array`
        The data points at which to evaluate the decomposition
    background : (Nb, d) `np.array`
        The data points at which to anchor the decomposition
    Imap_inv : List(List(int))
        A list of groups that represent a single feature. For instance `[[0, 1], [2]]` will treat
        the columns 1 and 2 as a single feature.
    
    Returns
    -------
    shapley_values : (Nf, n_features) `np.array`
    """

    # Setup Imap_inv
    Imap_inv, D, is_full_partition = check_Imap_inv(Imap_inv, background.shape[1])
    assert is_full_partition, "PermutationSHAP requires Imap_inv to be a partition of the input columns"
    N_eval = foreground.shape[0]
    if reversed:
        def permutation_generator(M, D):
            assert M%2==0, "Reversed Permutations require an even M"
            for _ in range(M//2):
                perm = np.random.permutation(D)
                yield perm
                yield perm[-1::-1]
    else:
        def permutation_generator(M, D):
            for _ in range(M):
                perm = np.random.permutation(D)
                yield perm
    
    # Compute the W matrix for SHAP value computations
    all_sets = dict({() : np.zeros(D)})
    # Go though all permutations to store all required sets
    for perm in permutation_generator(M, D):
        curr_set = []
        prev_tuple = None

        # Add the effect of the null coallition
        all_sets[()][perm[0]] -= 1
        
        # Going along the permutation
        for i in perm:
            curr_set.append(i)
            curr_set.sort()
            curr_tuple = tuple(curr_set)
            # Check if this curr_tuple has previously been used as key
            if curr_tuple not in all_sets:
                all_sets[curr_tuple] = np.zeros(D)
            all_sets[curr_tuple][i] += 1
            if prev_tuple is not None:
                all_sets[prev_tuple][i] -= 1
            # Keep track of the previous tuple
            prev_tuple = curr_tuple
    # All sets S on which we will evaluate nu(S)
    W = np.column_stack(list(all_sets.values())) / M
    interactions = list(all_sets.keys())
    S = len(interactions)

    # Setup arrays
    nu = np.zeros((S, N_eval))
    data_temp = np.copy(background)
    # Compute the weight matrix by visiting the lattice space
    for i, key in tqdm(enumerate(interactions), desc="Shapley Values", disable=not show_bar):
        cardinality = len(key)
        if cardinality == 0:
            nu[i] += h(background).mean()
        else:
            idx = ravel([Imap_inv[j] for j in key])
            for n in range(N_eval):
                data_temp[:, idx] = foreground[n, idx]
                nu[i][n] +=  h(data_temp).mean()
            # Reset the copied background
            data_temp[:,  idx] = background[:,  idx]

    if return_nu_evals:
        return W.dot(nu).T, S
    return W.dot(nu).T



def lattice_shap(h, foreground, background, interactions, Imap_inv=None, show_bar=True, return_nu_evals=False):
    """
    Approximate the Shapley Values of any black box given a subsample of the lattice-space

    Parameters
    ----------
    h : model X -> R
    foreground : (Nf, d) `np.array`
        The data points at which to evaluate the decomposition
    background : (Nb, d) `np.array`
        The data points at which to anchor the decomposition
    interactions : List(Tuple(int))
        List of tuples representing the lattice space.
    Imap_inv : List(List(int))
        A list of groups that represent a single feature. For instance `[[0, 1], [2]]` will treat
        the columns 1 and 2 as a single feature.
    
    Returns
    -------
    shapley_values : (Nf, n_features) `np.array`
    """
    
    assert type(interactions) == list, "interactions must be a list of tuples"
    assert () in interactions
    S = len(interactions)

    # Setup Imap_inv
    Imap_inv, D, is_full_partition = check_Imap_inv(Imap_inv, background.shape[1])
    assert is_full_partition, "LatticeSHAP requires Imap_inv to be a partition of the input columns"
    N_eval = foreground.shape[0]
    interactions_to_index = {interactions[i]:i for i in range(S)}

    # Setup arrays
    W = np.zeros((D, S))
    nu = np.zeros((S, N_eval))
    data_temp = np.copy(background)
    # Compute the weight matrix by visiting the lattice space
    for i, key in tqdm(enumerate(interactions), desc="Shapley Values", disable=not show_bar):
        cardinality = len(key)
        if cardinality == 0:
            nu[i] += h(background).mean()
        else:
            idx = ravel([Imap_inv[j] for j in key])
            for n in range(N_eval):
                data_temp[:, idx] = foreground[n, idx]
                nu[i][n] +=  h(data_temp).mean()
            # Reset the copied background
            data_temp[:,  idx] = background[:,  idx]
        if cardinality == 0:
            continue
        
        # Update the weight array
        W[key, i] += 1 / cardinality
        for subset in powerset(key):
            weight = (-1)**(cardinality - len(subset)) / cardinality
            W[key, interactions_to_index[subset]] += weight
    
    if return_nu_evals:
        return W.dot(nu).T, S
    return W.dot(nu).T



def get_PDP_PFI_importance(decomposition, show_bar=True):
    keys = decomposition.keys()
    additive_keys = [key for key in keys if len(key)==1]
    D = len(additive_keys)
    shape_decomposition = decomposition[additive_keys[0]].shape
    assert shape_decomposition[0] == shape_decomposition[1], "The decomposition must be anchored with foreground=background"
    I_PDP = np.zeros(D)
    I_PFI = np.zeros(D)
    for d in tqdm(range(D), desc="PDP/PFI Importance", disable=not show_bar):
        I_PDP[d] = np.mean(decomposition[additive_keys[d]].mean(1)**2)
        I_PFI[d] = np.mean(decomposition[additive_keys[d]].mean(0)**2)
    return I_PDP, I_PFI, additive_keys


def get_H_interaction(decomposition):
    keys = decomposition.keys()
    additive_keys = [key for key in keys if len(key)==1]
    D = len(additive_keys)
    shape_decomposition = decomposition[additive_keys[0]].shape
    assert shape_decomposition[0] == shape_decomposition[1], "The decomposition must be anchored with foreground=background"
    I_H  = np.zeros(D)
    for d in range(D):
        I_H[d]  = np.mean((decomposition[additive_keys[d]].mean(0) + \
                            decomposition[additive_keys[d]].mean(1))**2)
    return I_H, additive_keys


def get_h_add(decomposition, anchored=True):
    keys = decomposition.keys()
    additive_keys = [key for key in keys if len(key)==1]
    h_add = 0
    # Additive terms
    for key in additive_keys:
        if anchored:
            h_add += decomposition[key]
        else:
            h_add += decomposition[key][0]
    # Reference term
    if anchored:
        h_add += decomposition[()].reshape((1, -1))
    else:
        h_add += decomposition[()][0]
    return h_add



def get_CoE(decomposition, anchored=True):
    if anchored:
        factor = 100 / np.std(decomposition[()])
    else:
        factor = 100 / decomposition[()][1]
    h_add = get_h_add(decomposition, anchored)
    if anchored:
        return factor * np.sqrt( np.mean( (decomposition[()] - h_add.mean(1))**2 ) )
    else:
        return factor * np.sqrt( np.mean( (decomposition[()][0] - h_add)**2 ) )



#######################################################################################
#                                    Tree Ensembles
#######################################################################################    


def setup_treeshap(Imap_inv, foreground, background, model):
        # Setup Imap_inv
    Imap_inv, D, is_full_partition = check_Imap_inv(Imap_inv, background.shape[1])
    assert is_full_partition, "TreeSHAP requires Imap_inv to be a partition of the input columns"

    # Map Imap_inv through the pipeline
    if type(model) == Pipeline:
        preprocessing = model[:-1]
        model = model[-1]
        background = preprocessing.transform(background)
        foreground = preprocessing.transform(foreground)
        Imap_inv = get_Imap_inv_from_pipeline(Imap_inv, preprocessing)

    # Extract tree structure with the SHAP API
    masker = Independent(data=background, max_samples=background.shape[0])
    ensemble = Tree(model, data=masker).model
    
    # All numpy arrays must be C_CONTIGUOUS
    assert ensemble.thresholds.flags['C_CONTIGUOUS']
    assert ensemble.features.flags['C_CONTIGUOUS']
    assert ensemble.children_left.flags['C_CONTIGUOUS']
    assert ensemble.children_right.flags['C_CONTIGUOUS']
    assert ensemble.node_sample_weight.flags['C_CONTIGUOUS']
    
    # All arrays must be C-Contiguous and DataFrames are not.
    if type(foreground) == pd.DataFrame or not foreground.flags['C_CONTIGUOUS']:
        foreground = np.ascontiguousarray(foreground)
    if type(background) == pd.DataFrame or not background.flags['C_CONTIGUOUS']:
        background = np.ascontiguousarray(background)
    
    # Get Imap of the input features of the ensemble
    Imap = np.zeros(background.shape[1], dtype=np.int32)
    for i, group in enumerate(Imap_inv):
        for column in group:
            Imap[column] = i
    return Imap, foreground, background, model, ensemble



def interventional_treeshap(model, foreground, background, Imap_inv=None, anchored=False):
    """ 
    Compute the Interventional Shapley Values with the TreeSHAP algorithm

    Parameters
    ----------
    model : model_object
        The tree based machine learning model that we want to explain. XGBoost, LightGBM, CatBoost,
        and most tree-based scikit-learn models are supported.
    foreground : numpy.array or pandas.DataFrame
        The foreground dataset is the set of all points whose prediction we wish to explain.
    background : numpy.array or pandas.DataFrame
        The background dataset to use for integrating out missing features in the coallitional game.
    Imap_inv : List(List(int))
        A list of groups that represent a single feature. For instance `[[0, 1], [2]]` will treat
        the columns 1 and 2 as a single feature.

    Returns
    -------
    results : numpy.array
        A (N_foreground, n_features) array if `anchored=False`, otherwise a (N_foreground, N_background, n_features)
        array of anchored SHAP values.
    """

    sym = id(foreground) == id(background)

    # Setup
    Imap, foreground, background, model, ensemble = setup_treeshap(Imap_inv, foreground, background, model)
    
    # Shapes
    Nt = ensemble.features.shape[0]
    n_features = np.max(Imap) + 1
    depth = ensemble.features.shape[1]
    Nx = foreground.shape[0]
    Nz = background.shape[0]

    # Values at each leaf
    values = np.ascontiguousarray(ensemble.values[..., -1])

    # Where to store the output
    results = np.zeros((Nx, Nz, n_features)) if anchored else np.zeros((Nx, n_features)) 

    ####### Wrap C / Python #######

    # Find the shared library, the path depends on the platform and Python version    
    project_root = os.path.dirname(__file__).split('pyfd')[0]
    libfile = glob.glob(os.path.join(project_root, 'treeshap*.so'))[0]

    # Open the shared library
    mylib = ctypes.CDLL(libfile)

    # Tell Python the argument and result types of function main_treeshap
    mylib.main_int_treeshap.restype = ctypes.c_int
    mylib.main_int_treeshap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, 
                                        ctypes.c_int, ctypes.c_int,
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        ctypes.c_int, ctypes.c_int,
                                        np.ctypeslib.ndpointer(dtype=np.float64)]

    # 3. call function mysum
    mylib.main_int_treeshap(Nx, Nz, Nt, foreground.shape[1], depth, foreground, background, 
                            Imap, ensemble.thresholds, values,
                            ensemble.features, ensemble.children_left,
                            ensemble.children_right, anchored, sym, results)

    return results, ensemble





def taylor_treeshap(model, foreground, background, Imap_inv=None):
    """ 
    Compute the Shapley-Taylor interaction indices by adapting the the TreeSHAP algorithm

    Parameters
    ----------
    model : model_object
        The tree based machine learning model that we want to explain. XGBoost, LightGBM, CatBoost,
        and most tree-based scikit-learn models are supported.
    foreground : numpy.array or pandas.DataFrame
        The foreground dataset is the set of all points whose prediction we wish to explain.
    background : numpy.array or pandas.DataFrame
        The background dataset to use for integrating out missing features in the coallitional game.
    Imap_inv : List(List(int))
        A list of groups that represent a single feature. For instance `[[0, 1], [2]]` will treat
        the columns 1 and 2 as a single feature.

    Returns
    -------
    results : numpy.array
        A (N_foreground, n_features, n_features) array
    """

    # Setup
    Imap, foreground, background, model, ensemble = setup_treeshap(Imap_inv, foreground, background, model)
    
    # Shapes
    d = foreground.shape[1]
    n_features = np.max(Imap) + 1
    depth = ensemble.features.shape[1]
    Nx = foreground.shape[0]
    Nz = background.shape[0]
    Nt = ensemble.features.shape[0]

    # Values at each leaf
    values = np.ascontiguousarray(ensemble.values[..., -1]/Nz)

    # Where to store the output
    results = np.zeros((Nx, n_features, n_features))

    ####### Wrap C / Python #######

    # Find the shared library, the path depends on the platform and Python version    
    project_root = os.path.dirname(__file__).split('pyfd')[0]
    libfile = glob.glob(os.path.join(project_root, 'treeshap*.so'))[0]

    # Open the shared library
    mylib = ctypes.CDLL(libfile)

    # Tell Python the argument and result types of function main_treeshap
    mylib.main_taylor_treeshap.restype = ctypes.c_int
    mylib.main_taylor_treeshap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, 
                                        ctypes.c_int, ctypes.c_int,
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.float64)]

    # 3. call function mysum
    mylib.main_taylor_treeshap(Nx, Nz, Nt, d, depth, foreground, background, 
                                Imap, ensemble.thresholds, values,
                                ensemble.features, ensemble.children_left,
                                ensemble.children_right, results)

    return results, ensemble




def additive_treeshap(model, foreground, background, Imap_inv=None, anchored=False):
    """ 
    Compute the Additive Components of h by adapting the the TreeSHAP algorithm

    Parameters
    ----------
    model : model_object
        The tree based machine learning model that we want to explain. XGBoost, LightGBM, CatBoost,
        and most tree-based scikit-learn models are supported.
    foreground : numpy.array or pandas.DataFrame
        The foreground dataset is the set of all points whose prediction we wish to explain.
    background : numpy.array or pandas.DataFrame
        The background dataset to use for integrating out missing features in the coallitional game.
    Imap_inv : List(List(int))
        A list of groups that represent a single feature. For instance `[[0, 1], [2]]` will treat
        the columns 1 and 2 as a single feature.

    Returns
    -------
    results : numpy.array
        A (N_foreground, n_features) array if `anchored=False`, otherwise a (N_foreground, N_background, n_features)
        array of additive components.
    """

    sym = id(foreground) == id(background)
    # sym = False
    
    # Setup
    Imap, foreground, background, model, ensemble = setup_treeshap(Imap_inv, foreground, background, model)
    
    # Shapes
    d = foreground.shape[1]
    n_features = np.max(Imap) + 1
    depth = ensemble.features.shape[1]
    Nx = foreground.shape[0]
    Nz = background.shape[0]
    Nt = ensemble.features.shape[0]

    # Values at each leaf
    values = np.ascontiguousarray(ensemble.values[..., -1])

    # Where to store the output
    results = np.zeros((Nx, Nz, n_features)) if anchored else np.zeros((Nx, n_features)) 

    ####### Wrap C / Python #######

    # Find the shared library, the path depends on the platform and Python version    
    project_root = os.path.dirname(__file__).split('pyfd')[0]
    libfile = glob.glob(os.path.join(project_root, 'treeshap*.so'))[0]

    # Open the shared library
    mylib = ctypes.CDLL(libfile)

    # Tell Python the argument and result types of function main_treeshap
    mylib.main_add_treeshap.restype = ctypes.c_int
    mylib.main_add_treeshap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, 
                                        ctypes.c_int, ctypes.c_int,
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        ctypes.c_int, ctypes.c_int,
                                        np.ctypeslib.ndpointer(dtype=np.float64)]

    # 3. call function mysum
    mylib.main_add_treeshap(Nx, Nz, Nt, d, depth, foreground, background, 
                            Imap, ensemble.thresholds, values,
                            ensemble.features, ensemble.children_left,
                            ensemble.children_right, anchored, sym, results)

    return results, ensemble



def additive_leafshap(model, foreground, background, Imap_inv=None):
    
    # Setup
    Imap, foreground, background, model, ensemble = setup_treeshap(Imap_inv, foreground, background, model)
    
    # Shapes
    d = foreground.shape[1]
    n_features = np.max(Imap) + 1
    depth = ensemble.features.shape[1]
    Nx = foreground.shape[0]
    Nz = background.shape[0]
    Nt = ensemble.features.shape[0]

    # Values at each leaf
    values = np.ascontiguousarray(ensemble.values[..., -1])
    
    # Get the boundary boxes of all the leaves
    M, box_min, box_max = get_leaf_box(d, Nt, ensemble.features, ensemble.thresholds, 
                                       ensemble.children_left, ensemble.children_right)

    # Where to store the output
    results = np.zeros((Nx, n_features))

    ####### Wrap C / Python #######

    # Find the shared library, the path depends on the platform and Python version    
    project_root = os.path.dirname(__file__).split('pyfd')[0]
    libfile = glob.glob(os.path.join(project_root, 'treeshap*.so'))[0]

    # Open the shared library
    mylib = ctypes.CDLL(libfile)

    # Tell Python the argument and result types of function main_treeshap
    mylib.main_add_leafshap.restype = ctypes.c_int
    mylib.main_add_leafshap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, 
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.int32),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64),
                                        np.ctypeslib.ndpointer(dtype=np.float64)]

    # 3. call function mysum
    mylib.main_add_leafshap(Nx, Nz, Nt, d, depth, M, foreground, background, Imap,
                            values, ensemble.features, ensemble.children_left, 
                            ensemble.children_right, box_min, box_max, 
                            results)

    return results, ensemble



def additive_waterfallshap(model, foreground, background, Imap_inv=None):
    
    # Setup
    Imap, foreground, background, model, ensemble = setup_treeshap(Imap_inv, foreground, background, model)

    # Shapes
    d = foreground.shape[1]
    n_features = np.max(Imap) + 1
    depth = ensemble.features.shape[1]
    Nx = foreground.shape[0]
    Nz = background.shape[0]
    Nt = ensemble.features.shape[0]

    # Values at each leaf
    values = np.ascontiguousarray(ensemble.values[..., -1])
    
    # Get the boundary boxes of all the leaves
    max_depth = ensemble.max_depth

    # Where to store the output
    results = np.zeros((Nx, n_features))

    ####### Wrap C / Python #######

    # Find the shared library, the path depends on the platform and Python version    
    project_root = os.path.dirname(__file__).split('pyfd')[0]
    libfile = glob.glob(os.path.join(project_root, 'treeshap*.so'))[0]

    # Open the shared library
    mylib = ctypes.CDLL(libfile)

    # Tell Python the argument and result types of function main_treeshap
    mylib.main_add_waterfallshap.restype = ctypes.c_int
    mylib.main_add_waterfallshap.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            np.ctypeslib.ndpointer(dtype=np.float64),
                                            np.ctypeslib.ndpointer(dtype=np.float64),
                                            np.ctypeslib.ndpointer(dtype=np.int32),
                                            np.ctypeslib.ndpointer(dtype=np.float64),
                                            np.ctypeslib.ndpointer(dtype=np.float64),
                                            np.ctypeslib.ndpointer(dtype=np.int32),
                                            np.ctypeslib.ndpointer(dtype=np.int32),
                                            np.ctypeslib.ndpointer(dtype=np.int32),
                                            np.ctypeslib.ndpointer(dtype=np.float64),
                                            np.ctypeslib.ndpointer(dtype=np.float64)]

    # 3. call function mysum
    mylib.main_add_waterfallshap(Nx, Nz, Nt, d, depth, max_depth, foreground, background, Imap,
                            ensemble.thresholds, values, ensemble.features, ensemble.children_left, 
                            ensemble.children_right, ensemble.node_sample_weight, results)

    return results, ensemble



def decomposition_graph(decomposition, feature_names):
        
    def interpolate_rgb(rgb_list_1, rgb_list_2, interp):
        """ Linear interpolation interp * rbg_1 + (1 - interp) * rbg_2 """
        out = ''
        for color in range(3):
            hex_color = hex( round(interp * rgb_list_1[color] + \
                                   (1 - interp) * rgb_list_2[color]) )[2:]
            if len(hex_color) == 1:
                hex_color = '0' + hex_color
            out += hex_color
        return out

    # Directed Graph of partial ordering
    dot = Digraph(comment='Functional Decomposition', graph_attr={'ranksep': "0.75"},
                    node_attr={'shape': 'rectangle', 'color': 'black', 'style': 'filled'})
    U = [key for key in decomposition.keys() if len(key)>0]
    n_ranks = max([len(u) for u in U])
    ref_var = np.var(decomposition[()])
    var = {u : 100 * np.mean(decomposition[u].mean(1)**2) / ref_var for u in U}
    max_var = max(list(var.values()))
    ranks_set = []
    for _ in range(n_ranks):
        ranks_set.append(set())

    # Add each feature to the right set
    for u in U:
        ranks_set[ len(u)-1 ].add(u)
    
    # Print
    my_red = color_dict["DEEL"]["neg"]
    for elements in ranks_set:
        with dot.subgraph() as s:
            s.attr(rank='same')
            # Loop over all features of the same rank
            for u in elements:
                s.node(str(u), f":".join([feature_names[i] for i in u]) + "\n" + \
                               f"var={var[u]:.1f}%",
                        fillcolor=f'#{interpolate_rgb(my_red, [255] * 3, var[u]/max_var)}',
                        fontcolor='black')
                if len(u) > 1:
                    for u_subset in combinations(u, len(u)-1):
                        dot.edge(str(u_subset), str(u))
    
    
    return dot