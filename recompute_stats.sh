rm -r /mnt/data/repos/stats/
python compute_graph_stats.py --graphs-dir /mnt/data/repos/a-graphs/java/dp/ --lang java --out-dir /mnt/data/repos/stats/java/
python compute_graph_stats.py --graphs-dir /mnt/data/repos/a-graphs/csharp/dp/ --lang csharp --out-dir /mnt/data/repos/stats/csharp/
python compute_graph_stats.py --graphs-dir /mnt/data/repos/a-graphs/cpp/dp/ --lang cpp --out-dir /mnt/data/repos/stats/cpp/
cp  -r /mnt/data/repos/stats ~/Code/arcana-studies
