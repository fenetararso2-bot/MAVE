"""CLI: python crawl.py seeds.txt --pages 200 --depth 2 --delay 1.0 [--sitemaps] [--recrawl]"""
import argparse

from app.crawler import Crawler
from app.db import init_db

ap = argparse.ArgumentParser()
ap.add_argument("seeds", help="text file with one URL per line")
ap.add_argument("--pages", type=int, default=100)
ap.add_argument("--depth", type=int, default=2)
ap.add_argument("--delay", type=float, default=1.0)
ap.add_argument("--sitemaps", action="store_true", help="also queue URLs found in robots.txt / sitemap.xml")
ap.add_argument("--recrawl", action="store_true", help="re-fetch documents whose recrawl time has come")
ap.add_argument("--near-dup", type=int, default=3, help="SimHash distance for near-duplicate pages (0 = off)")
args = ap.parse_args()

init_db()
urls = [l.strip() for l in open(args.seeds, encoding="utf-8") if l.strip() and not l.startswith("#")]
crawler = Crawler(
    max_pages=args.pages, max_depth=args.depth, delay=args.delay,
    use_sitemaps=args.sitemaps, recrawl=args.recrawl, near_dup_distance=args.near_dup,
)
print(crawler.run(urls))
