rm -r /mnt/data/repos/stats/
python compute_graph_stats.py --graphs-dir /mnt/data/repos/graphs/java_v3.1/ --lang java --out-dir /mnt/data/repos/stats/java/
python compute_graph_stats.py --graphs-dir /mnt/data/repos/graphs/csharp/ --lang csharp --out-dir /mnt/data/repos/stats/csharp/
python compute_graph_stats.py --graphs-dir /mnt/data/repos/graphs/cpp/ --lang cpp --out-dir /mnt/data/repos/stats/cpp/
cp  -r /mnt/data/repos/stats ~/Code/arcana-studies
