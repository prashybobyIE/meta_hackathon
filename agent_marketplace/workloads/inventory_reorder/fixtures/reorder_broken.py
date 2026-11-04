def plan_reorder(payload):
    catalog = payload["catalog"]
    stock = payload["stock"]
    orders = []
    for sku in sorted(catalog):
        entry = catalog[sku]
        on_hand = stock.get(sku, 0)
        shortage = entry["reorder_point"] - on_hand
        if shortage > 0:
            packs = shortage // entry["pack_size"]
            orders.append({"sku": sku, "on_hand": on_hand, "packs": packs, "units": packs * entry["pack_size"]})
    return orders
