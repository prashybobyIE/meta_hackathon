def summarize_expenses(rows):
    total = 0
    by_category = {}
    count = 0
    for row in rows:
        amount = row.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ValueError("Amount must be integer cents")
        category = row.get("category") or "other"
        total += amount
        by_category[category] = by_category.get(category, 0) + amount
        count += 1
    return {"total_cents": total, "by_category": by_category, "count": count}
