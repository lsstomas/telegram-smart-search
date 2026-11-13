from decimal import Decimal as D

import pytest

from tgsearch.extract import extract_offer, parse_amount, payment_methods


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.299,90", D("1299.90")),
        ("1,299.90", D("1299.90")),
        ("1299,90", D("1299.90")),
        ("1299.9", D("1299.90")),
        ("1.299", D("1299.00")),
        ("10.000", D("10000.00")),
        ("1.234.567,89", D("1234567.89")),
        ("99", D("99.00")),
        ("12,5", D("12.50")),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_full_brazilian_offer(make_message):
    offer = extract_offer(
        make_message(
            "🔥 Notebook Lenovo IdeaPad 3\n"
            "De R$ 3.499,00 por R$ 2.799,00\n"
            "ou R$ 2.599 no Pix\n"
            "10x de R$ 279,90 sem juros\n"
            "Cupom: LENOVO10\n"
            "https://amzn.to/abc"
        )
    )
    assert offer.title == "Notebook Lenovo IdeaPad 3"
    assert offer.price == D("2799.00")
    assert offer.original_price == D("3499.00")
    assert offer.pix_price == D("2599.00")
    assert offer.best_price == D("2599.00")
    assert offer.currency == "BRL"
    assert offer.installments == 10
    assert offer.installment_value == D("279.90")
    assert offer.interest_free is True
    assert offer.coupon == "LENOVO10"
    assert offer.url == "https://amzn.to/abc"
    assert "pix" in offer.payment_methods


def test_installment_value_is_not_the_price(make_message):
    offer = extract_offer(make_message('Smart TV 50" em 10x de R$ 199,90'))
    assert offer.price is None
    assert offer.installments == 10
    assert offer.installment_value == D("199.90")
    assert offer.currency == "BRL"
    assert offer.has_pricing


def test_total_with_installments(make_message):
    offer = extract_offer(make_message("Monitor R$ 1.099 em 10x de R$ 109,90"))
    assert offer.price == D("1099.00")
    assert offer.installment_value == D("109.90")


def test_installments_without_value(make_message):
    offer = extract_offer(make_message("Monitor a partir de R$ 799 em até 4x sem juros"))
    assert offer.price == D("799.00")
    assert offer.installments == 4
    assert offer.installment_value is None
    assert offer.interest_free


def test_dimensions_are_not_installments(make_message):
    offer = extract_offer(make_message("Cabo 2x1 e suporte 4x4 por R$ 20"))
    assert offer.installments is None
    assert offer.price == D("20.00")


def test_pix_only_price(make_message):
    offer = extract_offer(make_message("SSD 1TB - R$ 349,90 à vista no pix"))
    assert offer.pix_price == D("349.90")
    assert offer.price == D("349.90")


def test_discount_shipping_and_coupon_amounts_are_ignored(make_message):
    offer = extract_offer(
        make_message(
            "Frete grátis acima de R$ 199! Cupom de R$ 50 off. Air fryer por apenas R$ 299"
        )
    )
    assert offer.price == D("299.00")
    assert offer.original_price is None


def test_original_price_must_be_higher(make_message):
    offer = extract_offer(make_message("De R$ 100 por R$ 150"))
    assert offer.price == D("150.00")
    assert offer.original_price is None


def test_us_format_and_code(make_message):
    offer = extract_offer(make_message("JBL headphones $1,299.99 code SAVE20"))
    assert offer.price == D("1299.99")
    assert offer.currency == "USD"
    assert offer.coupon == "SAVE20"


def test_reais_suffix(make_message):
    offer = extract_offer(make_message("Mouse gamer 89,90 reais"))
    assert offer.price == D("89.90")
    assert offer.currency == "BRL"


def test_no_price(make_message):
    offer = extract_offer(make_message("Bom dia! Live hoje às 20h"))
    assert offer.price is None
    assert not offer.has_pricing


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Pague no PIX ou boleto", ("pix", "boleto")),
        ("12x no cartão de crédito", ("credit_card",)),
        ("cartão de débito", ("debit_card",)),
        ("Aceita PayPal e Mercado Pago, PicPay", ("paypal", "mercado_pago", "picpay")),
        ("Pixel 8 por R$ 3.000", ()),
    ],
)
def test_payment_methods(text, expected):
    assert payment_methods(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Use o cupom PROMO15", "PROMO15"),
        ("cupom de desconto: black50", "BLACK50"),
        ("Código: TECH-2026", "TECH-2026"),
        ("Cupom de desconto aplicado no carrinho", None),
        ("ganhe cupom aqui", None),
    ],
)
def test_coupon(make_message, text, expected):
    assert extract_offer(make_message(text)).coupon == expected


def test_telegram_links_are_not_offer_urls(make_message):
    offer = extract_offer(
        make_message(
            "Entre no grupo https://t.me/joinchat/xyz e compre https://loja.com/p/1 por R$ 10"
        )
    )
    assert offer.url == "https://loja.com/p/1"


def test_entity_url_is_preferred(make_message):
    offer = extract_offer(make_message("Comprar R$ 10", urls=["https://loja.com/hidden"]))
    assert offer.url == "https://loja.com/hidden"
