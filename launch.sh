pip install uv
uv venv
source .venv/bin/activate
uv sync
uv pip list
uv run python pyfd/utils.py