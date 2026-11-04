def parse_catalog(text):
    lines = text.split("\n")
    if lines[0].strip() != "sku;reorder_point;pack_size":
        raise ValueError("Expected sku;reorder_point;pack_size header")
    table = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(";")
        if len(parts) != 3:
            raise ValueError("Expected three fields")
        sku = parts[0].strip()
        point = parts[1].strip()
        pack = parts[2].strip()
        if not sku or sku in table:
            raise ValueError("Blank or duplicate sku")
        if not point.isascii() or not point.isdigit() or not pack.isascii() or not pack.isdigit():
            raise ValueError("Quantities must be non-negative integers")
        if int(pack) < 1:
            raise ValueError("Pack size must be positive")
        table[sku] = {"reorder_point": int(point), "pack_size": int(pack)}
    return table
