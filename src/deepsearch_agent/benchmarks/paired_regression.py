"""Stable offline CLI entrypoint."""

if __name__ == "__main__":
    from runpy import run_module
    run_module('deepsearch_experiments.benchmarks.paired_regression', run_name="__main__")
