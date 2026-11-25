"""mem_fts 全量重建：切换分词器（assembler.tokenizer）或索引漂移修复时运行。

用法：uv run scripts/rebuild_fts.py [--settings config/settings.yaml]
"""
import argparse
from contextlib import closing
from pathlib import Path

from pismor.core import fts, tokenizer
from pismor.core.config import load_settings
from pismor.core.db import get_conn, init_schema


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=Path("config/settings.yaml"))
    args = parser.parse_args(argv)
    settings = load_settings(args.settings)
    tokenizer.set_tokenizer(settings.assembler.tokenizer)
    with closing(get_conn(settings.db_path)) as conn:
        init_schema(conn, None)  # meta 对齐（vec_dimensions=None 不触碰 embeddings）
        fts.rebuild(conn)
        n = conn.execute("SELECT count(*) FROM mem_fts").fetchone()[0]
    print(f"mem_fts rebuilt: {n} rows (tokenizer={tokenizer.current()})")


if __name__ == "__main__":
    main()
