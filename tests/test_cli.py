import csv
import io

from tgsearch.cli import main


def _read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_export_end_to_end(fixtures, tmp_path, capsys):
    out = tmp_path / "out.csv"
    code = main(["export", "-f", str(fixtures / "export_single.json"), "notebook", "-o", str(out)])
    assert code == 0
    rows = _read(out)
    by_id = {r["message_id"]: r for r in rows}

    # 10 and 11 are the same deal (same URL after tracking removal, price, coupon)
    assert "11" not in by_id
    assert by_id["10"]["duplicates"] == "1"
    assert by_id["10"]["also_seen"] == "https://t.me/c/1234567890/11"
    assert by_id["10"]["best_price"] == "2599.00"
    # 12 is the same product at a new price: kept
    assert by_id["12"]["best_price"] == "2399.00"
    # 14 has no price, 13 does not match "notebook"
    assert "14" not in by_id and "13" not in by_id
    assert "15" in by_id
    assert by_id["16"]["text"].startswith("'=")
    assert "1 duplicates merged" in capsys.readouterr().err


def test_affiliate_tag_difference_is_not_merged(fixtures, tmp_path):
    out = tmp_path / "out.csv"
    main(
        [
            "export",
            "-f",
            str(fixtures / "export_single.json"),
            "-f",
            str(fixtures / "export_full.json"),
            "lenovo",
            "-o",
            str(out),
            "-q",
        ]
    )
    rows = _read(out)
    # Promo Group post has no affiliate tag; it is kept apart from msg 10 but absorbs nothing
    chats = sorted(r["chat"] for r in rows)
    assert chats == ["Ofertas Tech", "Ofertas Tech", "Promo Group"]


def test_no_dedupe_and_price_filter(fixtures, tmp_path):
    out = tmp_path / "out.csv"
    main(
        [
            "export",
            "-f",
            str(fixtures / "export_single.json"),
            "notebook",
            "--no-dedupe",
            "--max-price",
            "2.600",
            "--sort",
            "price",
            "-o",
            str(out),
            "-q",
        ]
    )
    prices = [r["best_price"] for r in _read(out)]
    assert prices == ["999.00", "2399.00", "2599.00", "2599.00"]


def test_stdout_and_delimiter(fixtures, capsys):
    assert (
        main(
            ["export", "-f", str(fixtures / "export_single.json"), "ssd", "--delimiter", ";", "-q"]
        )
        == 0
    )
    captured = capsys.readouterr()
    rows = list(csv.DictReader(io.StringIO(captured.out), delimiter=";"))
    assert len(rows) == 1
    assert rows[0]["payment_methods"] == "boleto;credit_card"
    assert captured.err == ""


def test_date_filters(fixtures, tmp_path):
    out = tmp_path / "out.csv"
    main(
        [
            "export",
            "-f",
            str(fixtures / "export_single.json"),
            "notebook",
            "--since",
            "2026-10-04",
            "--until",
            "2026-10-07",
            "-o",
            str(out),
            "-q",
        ]
    )
    assert sorted(r["message_id"] for r in _read(out)) == ["12", "15"]


def test_missing_file(tmp_path, capsys):
    assert main(["export", "-f", str(tmp_path / "nope.json"), "x"]) == 1
    assert "nope.json" in capsys.readouterr().err


def test_live_without_credentials_fails_cleanly(tmp_path, capsys):
    assert main(["live", "-c", "@ofertas", "ssd"]) == 2
    assert "TELEGRAM_API_ID" in capsys.readouterr().err
    assert not (tmp_path / "xdg" / "tgsearch").exists()  # no session dir created
