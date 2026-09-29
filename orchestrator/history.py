"""Private, local purchase history. No credentials or browser state enter this DB."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .enricher import normalize


COMPLETE = {"completed", "delivered", "complete", "concluido", "concluído", "entregue", "finalizado"}


def default_path() -> Path:
    if os.environ.get("ANDORINHA_DATA_DIR"):
        return Path(os.environ["ANDORINHA_DATA_DIR"]) / "purchases.sqlite3"
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return root / "AndorinhaShopping" / "purchases.sqlite3"


class PurchaseHistory:
    def __init__(self, path: Path | None = None):
        self.path = path or default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY, ordered_at TEXT NOT NULL,
                status TEXT NOT NULL, source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_lines (
                order_id TEXT NOT NULL, line_no INTEGER NOT NULL,
                product_id TEXT, name TEXT NOT NULL, brand TEXT,
                quantity REAL NOT NULL, unit TEXT, unit_price REAL,
                PRIMARY KEY (order_id, line_no),
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            );
            CREATE TABLE IF NOT EXISTS corrections (
                item_key TEXT PRIMARY KEY, product_id TEXT, name TEXT NOT NULL,
                corrected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lines_product ON order_lines(product_id);
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def counts(self) -> dict[str, int]:
        return {
            "orders": self.db.execute("SELECT count(*) FROM orders").fetchone()[0],
            "lines": self.db.execute("SELECT count(*) FROM order_lines").fetchone()[0],
            "corrections": self.db.execute("SELECT count(*) FROM corrections").fetchone()[0],
        }

    def import_orders(self, orders: list[dict[str, Any]], source: str) -> dict[str, int]:
        added = skipped = 0
        with self.db:
            for order in orders:
                order_id = str(order.get("order_id") or order.get("id") or "").strip()
                ordered_at = str(order.get("ordered_at") or order.get("date") or "").strip()
                status = normalize(str(order.get("status") or ""))
                lines = order.get("items") or order.get("lines") or []
                if not order_id or not ordered_at or status not in {normalize(x) for x in COMPLETE} or not isinstance(lines, list) or not lines:
                    skipped += 1
                    continue
                if self.db.execute("SELECT 1 FROM orders WHERE order_id=?", (order_id,)).fetchone():
                    skipped += 1
                    continue
                valid = []
                for line in lines:
                    if not isinstance(line, dict):
                        continue
                    name = str(line.get("name") or line.get("product_name") or "").strip()
                    if not name:
                        continue
                    try:
                        qty = float(line.get("quantity") or line.get("qty") or 1)
                        price = line.get("unit_price")
                        price = float(price) if price not in (None, "") else None
                    except (TypeError, ValueError):
                        continue
                    if qty <= 0:
                        continue
                    valid.append((str(line.get("product_id") or line.get("sku") or "") or None,
                                  name, str(line.get("brand") or "") or None, qty,
                                  str(line.get("unit") or "") or None, price))
                if not valid:
                    skipped += 1
                    continue
                self.db.execute("INSERT INTO orders VALUES (?, ?, ?, ?)", (order_id, ordered_at, status, source))
                self.db.executemany(
                    "INSERT INTO order_lines VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(order_id, n, *line) for n, line in enumerate(valid)],
                )
                added += 1
        return {"added": added, "skipped": skipped, **self.counts()}

    def import_file(self, path: Path) -> dict[str, int]:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            orders = data.get("orders") if isinstance(data, dict) else data
        elif path.suffix.lower() == ".csv":
            with path.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            grouped: dict[str, dict] = {}
            for row in rows:
                oid = str(row.get("order_id") or row.get("pedido_id") or "").strip()
                if not oid:
                    continue
                order = grouped.setdefault(oid, {
                    "order_id": oid,
                    "ordered_at": row.get("ordered_at") or row.get("data"),
                    "status": row.get("status") or row.get("situacao"),
                    "items": [],
                })
                order["items"].append({
                    "product_id": row.get("product_id") or row.get("sku"),
                    "name": row.get("name") or row.get("produto"),
                    "brand": row.get("brand") or row.get("marca"),
                    "quantity": row.get("quantity") or row.get("quantidade"),
                    "unit": row.get("unit") or row.get("unidade"),
                    "unit_price": row.get("unit_price") or row.get("preco_unitario"),
                })
            orders = list(grouped.values())
        else:
            raise ValueError("Use um arquivo JSON ou CSV")
        if not isinstance(orders, list):
            raise ValueError("O arquivo deve conter uma lista de pedidos")
        return self.import_orders(orders, source=f"file:{path.name}")

    def correct(self, item: str, candidate: dict) -> None:
        name = str(candidate.get("name") or "").strip()
        if not name:
            raise ValueError("Produto sem nome")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO corrections VALUES (?, ?, ?, ?)",
                (normalize(item), str(candidate.get("product_id") or "") or None,
                 name, datetime.now().isoformat(timespec="seconds")),
            )

    def related(self, item: str, limit: int = 5, before: str | None = None) -> list[dict]:
        """Return bounded evidence; old orders never leak into a replay."""
        key = normalize(item)
        words = {w for w in key.split() if len(w) >= 3}
        if not words:
            return []
        query = """SELECT l.product_id, l.name, l.brand, l.quantity, l.unit,
                          l.unit_price, o.ordered_at
                   FROM order_lines l JOIN orders o USING(order_id)
                   WHERE (? IS NULL OR o.ordered_at < ?)
                   ORDER BY o.ordered_at DESC LIMIT 1000"""
        rows = self.db.execute(query, (before, before)).fetchall()
        scored = []
        for row in rows:
            name_words = set(normalize(row["name"]).split())
            overlap = len(words & name_words) / len(words)
            if overlap >= 0.75:
                scored.append((overlap, dict(row)))
        scored.sort(key=lambda pair: (pair[0], pair[1]["ordered_at"]), reverse=True)
        evidence = [row for _, row in scored[:limit]]
        correction = self.db.execute("SELECT product_id, name, corrected_at FROM corrections WHERE item_key=?", (key,)).fetchone()
        if correction and before is None:
            evidence.insert(0, {"product_id": correction["product_id"], "name": correction["name"],
                                "ordered_at": correction["corrected_at"], "source": "correction"})
        return evidence[:limit]
