```bash
# Just Java, default settings
python download_repos.py --lang java

# All three languages, skip tiny repos, cap size, shuffle order
python download_repos.py --lang all --min-loc 5000 --max-size-kb 200000 --shuffle

# Preview what a filtered set looks like before committing
python download_repos.py --lang java c# --min-stars 500 --dry-run

# Download only, extract later (e.g. on a machine with more cores)
python download_repos.py --lang java --no-extract
```
