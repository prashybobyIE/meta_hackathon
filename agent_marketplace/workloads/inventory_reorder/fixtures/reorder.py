def plan_reorder(payload):
    catalog = payload["catalog"]
    stock = payload["stock"]
    for sku in stock:
        on_hand = stock[sku]
        if sku not in catalog:
            raise ValueError("Stock lists an unknown sku")
        if isinstance(on_hand, bool) or not isinstance(on_hand, int) or on_hand < 0:
            raise ValueError("Stock must be a non-negative integer")
    orders = []
    for sku in sorted(catalog):
        entry = catalog[sku]
        on_hand = stock.get(sku, 0)
        shortage = entry["reorder_point"] - on_hand
        if shortage > 0:
            packs = (shortage + entry["pack_size"] - 1) // entry["pack_size"]
            orders.append({"sku": sku, "on_hand": on_hand, "packs": packs, "units": packs * entry["pack_size"]})
    return orders
