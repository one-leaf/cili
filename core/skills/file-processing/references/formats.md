# Data & misc formats (file-processing)

## CSV / TSV

Use:

```text
pandas
```

for structured/tabular CSV processing.

```python
import pandas as pd

df = pd.read_csv(path)
```

For very large CSV files, do not load the entire file into memory.

Use chunked processing:

```python
for chunk in pd.read_csv(path, chunksize=100000):
    process(chunk)
```

For simple line-oriented processing where pandas is unnecessary, Python's built-in `csv` module is sufficient.

---

## HTML

For HTML parsing:

### Primary

```text
BeautifulSoup4
```

```python
from bs4 import BeautifulSoup
```

For complex HTML extraction, consider:

```text
lxml
```

Do not use regular expressions as the primary HTML parser.

---

## XML

Prefer:

### Standard XML

Python's built-in:

```python
import xml.etree.ElementTree as ET
```

For more advanced XML processing:

```text
lxml
```

Use `lxml` when:

* XPath is required
* namespaces are complex
* performance matters
* advanced XML processing is needed

---

## JSON

Use Python's built-in:

```python
import json
```

Do not install a third-party package unnecessarily.

---

## Images

For image metadata and basic image processing:

```text
Pillow
```

Import:

```python
from PIL import Image
```

Use Pillow for:

* opening images
* resizing
* format conversion
* metadata
* basic image processing

For OCR, use an OCR-specific library rather than Pillow alone.

---

## ZIP / Archive

Use Python's standard library whenever possible:

```python
import zipfile
```

Do not install third-party packages unless the requested archive format requires them.

---

## Large File Processing

When processing large files, do not unnecessarily load the entire file into memory.

### PDF

Process page-by-page:

```python
for page in doc:
    process(page)
```

### Excel

For large XLSX files, consider:

```python
openpyxl.load_workbook(
    path,
    read_only=True,
    data_only=True
)
```

For large tabular data, prefer:

```python
pd.read_csv(
    path,
    chunksize=100000
)
```

### DOCX

Process paragraphs/tables incrementally where practical.
