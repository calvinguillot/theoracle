# uv's install folder - Git Bash does not always pick it up from Windows
export PATH="$HOME/.local/bin:$PATH"

# TouchDesigner's Python. Used ONCE, to create .venv - see Step 7.
export TD_PYTHON="C:/Program Files/Derivative/TouchDesigner/bin/python.exe"

# Never let uv download its own Python.
export UV_PYTHON_DOWNLOADS=never

# Never set UV_PYTHON. `uv pip` treats it as the place to install INTO,
# overriding the active .venv - it would try to install into TouchDesigner itself.
unset UV_PYTHON
