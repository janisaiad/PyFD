import numpy as np
from copy import deepcopy
from itertools import chain, combinations
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, MinMaxScaler, KBinsDiscretizer, FunctionTransformer
from sklearn.preprocessing import OneHotEncoder, SplineTransformer, QuantileTransformer

ADDITIVE_TRANSFORMS = [StandardScaler, MinMaxScaler, QuantileTransformer,
                       FunctionTransformer, KBinsDiscretizer, SplineTransformer, OneHotEncoder]


def powerset(iterable):
    "powerset([1,2,3]) --> () (1,) (2,) (3,) (1,2) (1,3) (2,3)"
    s = list(iterable)
    return chain.from_iterable(combinations(s, r) for r in range(len(s)))


def ravel(tuples):
    """
    turn a tuple of tuples ((a, b), (c,)) into a single tuple (a, b, c)
    turn a list of list [[a, b], [c]] into a single list [a, b, c]
    """
    out = []
    for seq_tuple in tuples:
        for element in seq_tuple:
            out.append(element)
    return out



def check_Imap_inv(Imap_inv, d):
    if Imap_inv is None:
        Imap_inv = [[i] for i in range(d)]
        is_full_partition = True
    else:
        assert type(Imap_inv) in (list, tuple), "Imap_inv must be a list or a tuple"
        assert type(Imap_inv[0]) in (list, tuple), "Elements of Imap_inv must be lists or tuples"
        raveled_Imap_inv = sorted(ravel(Imap_inv))
        assert min(raveled_Imap_inv) >= 0, "Imap_inv must have positive values"
        assert max(raveled_Imap_inv) < d, "Imap_inv cannot exceed the value of features"
        # Is the Imap_inv a full partition of the input space ?
        is_full_partition = raveled_Imap_inv == list(range(d))
    D = len(Imap_inv)
    return Imap_inv, D, is_full_partition



def get_Imap_inv_from_pipeline(Imap_inv, pipeline):
    Imap_inv_copy = deepcopy(Imap_inv)
    Imap_inv_return = deepcopy(Imap_inv)
    # Iterate over the whole pipeline
    for layer in pipeline:
        # Only certain layers are supported
        assert type(layer) in ADDITIVE_TRANSFORMS or type(layer) == ColumnTransformer

        # Compute the list of transformers and the columns they act on
        if type(layer) in [SplineTransformer, OneHotEncoder]:
            transformers = [('layer', layer, range(layer.n_features_in_))]
        elif type(layer) == KBinsDiscretizer and layer.encode in ['onehot', 'onehot-dense']:
            transformers = [('layer', layer, range(layer.n_features_in_))]
        elif type(layer) == ColumnTransformer:
            transformers = layer.transformers_
        else:
            transformers = None

        # If transformers increase the number of columns, we must change Imap_inv_copy
        # This id done by composing Imap_inv_copy with Imap_inv_layer
        if transformers is not None:
            # We compute the Imap_inv relative to this layer
            Imap_inv_layer = [[] for _ in range(layer.n_features_in_)]

            curr_idx = 0
            for transformer in transformers:
                # Splines maps a column to dim columns
                if type(transformer[1]) == SplineTransformer:            
                    degree = transformer[1].degree
                    n_knots = transformer[1].n_knots
                    dim = n_knots + degree - 2
                    # Iterate over all columns the transformer acts upon
                    for i in transformer[2]:
                        Imap_inv_layer[i] = list(range(curr_idx, curr_idx+dim))
                        curr_idx += dim
                # OHE maps a column to n_categories columns
                elif type(transformer[1]) == OneHotEncoder:
                    # Iterate over all columns the transformer acts upon
                    for idx, i in enumerate(transformer[2]):
                        n_categories = len(transformer[1].categories_[idx])
                        Imap_inv_layer[i] = list(range(curr_idx, curr_idx+n_categories))
                        curr_idx += n_categories
                # BinsDiscretizer maps a column to n_bins columns
                elif type(transformer[1]) == KBinsDiscretizer and \
                          transformer[1].encode in ['onehot', 'onehot-dense']:
                    # Iterate over all columns the transformer acts upon
                    for idx, i in enumerate(transformer[2]):
                        n_bins = transformer[1].n_bins_[idx]
                        Imap_inv_layer[i] = list(range(curr_idx, curr_idx+n_bins))
                        curr_idx += n_bins
                # Other transformers map a column to a column
                else:
                    for i in transformer[2]:
                        Imap_inv_layer[i] = [curr_idx]
                        curr_idx += 1

            # We map Imap_inv through the Imap_inv_layer
            for i, group in enumerate(Imap_inv_copy):
                Imap_inv_return[i] = []
                for j in group:
                    Imap_inv_return[i] += Imap_inv_layer[j]
            Imap_inv_copy = deepcopy(Imap_inv_return)
    
    return Imap_inv_return



def get_leaf_box(d, Nt, features, thresholds, left_child, right_child):
    global leaf_id

    M = np.max(np.sum(features<0, axis=-1))
    all_boxes = np.zeros((Nt, M, d, 2))
    for t in range(Nt):
        leaf_id = 0
        box = np.array([[-np.inf, np.inf] for _ in range(d)])
        leaf_box_recurse(box, 0, features[t], thresholds[t], left_child[t], right_child[t], all_boxes[t])

    boxes_min = np.ascontiguousarray(all_boxes[..., 0])
    boxes_max = np.ascontiguousarray(all_boxes[..., 1])
    return M, boxes_min, boxes_max


def leaf_box_recurse(box, node, features, thresholds, left_child, right_child, all_boxes):
    global leaf_id

    # Current state
    curr_feature = features[node]
    curr_threshold = thresholds[node]
    curr_left_child = left_child[node]
    curr_right_child = right_child[node]

    # Is a leaf
    if (curr_feature < 0):
        all_boxes[leaf_id] = box
        leaf_id += 1
    else:
        # Going left
        curr_box = deepcopy(box)
        if curr_threshold < box[curr_feature, 1]:
            curr_box[curr_feature, 1] = curr_threshold
        leaf_box_recurse(curr_box, curr_left_child, features, thresholds, left_child, right_child, all_boxes)
        
        # Going right
        curr_box = deepcopy(box)
        if box[curr_feature, 0] < curr_threshold:
            curr_box[curr_feature, 0] = curr_threshold
        leaf_box_recurse(curr_box, curr_right_child, features, thresholds, left_child, right_child, all_boxes)