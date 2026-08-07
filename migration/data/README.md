# migration/data/

`database_parser.py` writes `products.json` and `customers.json` here.
They're intermediate artifacts consumed by `csv_generator.py`.

Git-ignored — both files contain real customer PII sourced from `dump.sql`.
