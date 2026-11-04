def categorize_expenses(rows):
    result = []
    for row in rows:
        item = dict(row)
        category = (item.get("category") or "").strip().lower()
        merchant = (item.get("merchant") or "").lower()
        if not category:
            if any([word in merchant for word in ["coffee", "cafe", "restaurant"]]):
                category = "food"
            elif any([word in merchant for word in ["uber", "train", "bus"]]):
                category = "transport"
            else:
                category = "other"
        item["category"] = category
        result.append(item)
    return result
