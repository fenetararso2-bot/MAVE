"""Compare search configurations on a labelled dataset (no database needed).

    python eval_search.py                          # seed dataset, all configurations
    python eval_search.py --lang om                # only Afaan Oromoo queries
    python eval_search.py --dataset my_labels.json -k 10 --verbose
"""
import argparse
import sys
from pathlib import Path

from app.search.evaluation import DEFAULT_CONFIGS, compare, format_table, load_dataset

DEFAULT_DATASET = Path(__file__).parent / "eval" / "seed_dataset.json"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", default=str(DEFAULT_DATASET))
    ap.add_argument("--lang", help="only queries with this language (om, en)")
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--verbose", action="store_true", help="per-query nDCG for every configuration")
    args = ap.parse_args(argv)

    data = load_dataset(args.dataset)
    if data.get("_note"):
        print("NOTE:", data["_note"], "\n")
    results = compare(data, DEFAULT_CONFIGS, args.k, args.lang)
    print(format_table(results))
    if args.verbose:
        for r in results:
            print(f"\n[{r['config']}]")
            for p in r["per_query"]:
                print(f"  {p['ndcg']:.3f}  {p['q']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
