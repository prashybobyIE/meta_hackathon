import csv
import io
import decimal

def parse_expenses(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    fields = ["date", "merchant", "amount", "category"]
    if reader.fieldnames != fields:
        raise ValueError("Expected date,merchant,amount,category headers")
    result = []
    for row in reader:
        if None in row or len(row) != 4 or any([row[key] is None for key in fields]):
            raise ValueError("Malformed CSV row")
        text = row["amount"].strip()
        number = text
        if number.startswith("+") or number.startswith("-"):
            number = number[1:]
        parts = number.split(".")
        if len(parts) > 2 or not parts[0] or not parts[0].isascii() or not parts[0].isdigit():
            raise ValueError("Malformed amount")
        if len(parts) == 2 and (not parts[1] or len(parts[1]) > 2 or not parts[1].isascii() or not parts[1].isdigit()):
            raise ValueError("Malformed amount")
        amount = decimal.Decimal(text)
        item = dict(row)
        item["amount"] = int(amount * 100)
        result.append(item)
    return result
