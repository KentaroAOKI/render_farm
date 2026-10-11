"""One-time result migration, or compatibility bridge for a cached worker."""
import argparse
from pathlib import Path
import sys
import os

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from farm.common import runtime_env
os.environ.update(runtime_env(ROOT))
from farm.results import bridge, migrate_results
from farm.store import Store

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch-pid', type=int)
    args = parser.parse_args()
    if args.watch_pid:
        bridge(ROOT, args.watch_pid)
    else:
        print(migrate_results(Store(ROOT)))
