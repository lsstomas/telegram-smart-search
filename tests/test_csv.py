import csv
import io

from tgsearch.csv_out import COLUMNS, write_csv
from tgsearch.extract import extract_offer


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_columns_and_values(make_message):
    offer = extract_offer(
        make_message(
            "Notebook\nDe R$ 3.499 por R$ 2.799\n10x de R$ 279,90 sem juros, pix ou boleto",
            link="https://t.me/canal/1",
        )
    )
    buf = io.StringIO()
    assert write_csv([offer], buf) == 1
    out = buf.getvalue()
    assert out.splitlines()[0] == ",".join(COLUMNS)
    row = _rows(out)[0]
    assert row["price"] == "2799.00"
    assert row["original_price"] == "3499.00"
    assert row["installments"] == "10"
    assert row["installment_value"] == "279.90"
    assert row["interest_free"] == "yes"
    assert row["payment_methods"] == "pix;boleto"
    assert row["link"] == "https://t.me/canal/1"
    assert "\n" not in row["text"]


def test_formula_injection_is_neutralised(make_message):
    offer = extract_offer(make_message('=HYPERLINK("http://x") Mouse R$ 10'))
    buf = io.StringIO()
    write_csv([offer], buf)
    row = _rows(buf.getvalue())[0]
    assert row["text"].startswith("'=")


def test_semicolon_delimiter(make_message):
    buf = io.StringIO()
    write_csv([extract_offer(make_message("Mouse R$ 10"))], buf, delimiter=";")
    assert buf.getvalue().splitlines()[0].startswith("date;chat;")
