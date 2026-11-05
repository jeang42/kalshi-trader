"""User-defined strategy plugins.

Drop a Python file in this directory with one or more
``@backtest.register_strategy(key, display_name)``-decorated functions
and they'll appear in the Backtest page's strategy dropdown on the
next app reload. See ``example_buy_low_yes.py`` for the minimal
shape; see ``backtest.py`` (``register_strategy`` docstring) for the
full contract.

Files prefixed with ``_`` are skipped by the discovery loop — useful
for shared helpers you don't want to register as standalone
strategies.

**Trust model**: user plugins run in the same Python process as the
rest of the app. There's no sandboxing. Don't drop in code you
haven't read.
"""
