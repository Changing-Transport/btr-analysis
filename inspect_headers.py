import openpyxl

def inspect(path, country):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    print(f"{country} | all sheets: {wb.sheetnames}")
    # Try table5 variants first, then fall back to any sheet with '5' in name
    target = None
    for name in wb.sheetnames:
        n = name.strip().lower().replace(" ", "")
        if (n.startswith("table5") or n.startswith("tableau5") or
                n.startswith("tabela5") or n == "ctf-five" or n == "ctffive"):
            target = name
            break
    if target is None:
        print("  (no table5 match found)")
        wb.close()
        return
    ws = wb[target]
    print(f"  using sheet: {target}")
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if any(c is not None for c in row):
            cells = [(chr(65+j), str(v)[:38]) for j, v in enumerate(row) if v is not None]
            print(f"  row {i}: {cells}")
        if i >= 6:
            break
    wb.close()

files = [
    (r"downloaded_xlsx\Armenia.xlsx", "Armenia"),
    (r"downloaded_xlsx\Australia.xlsx", "Australia"),
    (r"downloaded_xlsx\Austria.xlsx", "Austria"),
    (r"downloaded_xlsx\Bahrain.xlsx", "Bahrain"),
    (r"downloaded_xlsx\Azerbaijan.xlsx", "Azerbaijan"),
    (r"downloaded_xlsx\Trinidad_and_Tobago.xlsx", "Trinidad and Tobago"),
]

for path, country in files:
    try:
        inspect(path, country)
        print()
    except Exception as e:
        print(f"{country}: {e}")
        print()
