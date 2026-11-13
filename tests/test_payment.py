from decimal import Decimal as D

from tgsearch.payment import parse_terms


def _points(text):
    return [
        (p.method, p.amount, p.installments, p.installment_value, p.interest, p.source)
        for p in parse_terms(text).points
    ]


def test_full_brazilian_post():
    text = (
        "🔥 Placa de Vídeo RX 7600 8GB\n"
        "De R$ 2.199,00\n"
        "💠 R$ 1.599,99 no Pix\n"
        "💳 ou R$ 1.799,99 em até 10x sem juros\n"
        "🎟 Cupom: GAMER10 (R$ 50 OFF)\n"
    )
    terms = parse_terms(text)
    assert _points(text) == [
        ("pix", D("1599.99"), None, None, "", "stated"),
        ("installments", D("1799.99"), 10, None, "free", "stated"),
    ]
    assert terms.original_price == D("2199.00")
    assert terms.coupons == ("GAMER10",)
    assert terms.coupon_discounts == ("R$ 50.00",)


def test_installments_with_interest_and_rate_are_computed():
    text = "RTX 5050\nPor R$ 1.699 à vista\nou 12x de R$ 158,25 com juros (1,99% a.m.)"
    points = parse_terms(text).points
    assert [(p.method, p.amount) for p in points] == [
        ("upfront", D("1699.00")),
        ("installments", D("1899.00")),
    ]
    inst = points[1]
    assert (inst.installments, inst.installment_value, inst.interest) == (
        12,
        D("158.25"),
        "charged",
    )
    assert inst.monthly_rate == D("1.99") and inst.source == "computed"


def test_stated_total_replaces_computed_total():
    text = "RTX 5050 R$ 1.649,90\n10x R$ 164,99 sem juros (total R$ 1.649,90)"
    assert _points(text)[1] == ("installments", D("1649.90"), 10, D("164.99"), "free", "stated")


def test_coupon_required_price_and_card_line():
    terms = parse_terms("RX 7600\nCom cupom: R$ 1.449\nCartão: R$ 1.529")
    assert [(p.method, p.amount, p.coupon_applied) for p in terms.points] == [
        ("unspecified", D("1449.00"), True),
        ("card_1x", D("1529.00"), False),
    ]


def test_price_then_coupon_on_same_line():
    terms = parse_terms("💰 R$ 1.799,99 no Pix com cupom: SAVE10")
    assert [(p.method, p.amount, p.coupon_applied) for p in terms.points] == [
        ("pix", D("1799.99"), True)
    ]
    assert terms.coupons == ("SAVE10",)


def test_coupon_discount_is_not_a_price():
    terms = parse_terms("RX 7600 R$ 1.799\n🎟 com cupom de R$ 100 off\nR$ 300 off")
    assert [p.amount for p in terms.points] == [D("1799.00")]
    assert terms.coupon_discounts == ("R$ 100.00",)


def test_pix_named_on_another_line_applies_to_the_only_price():
    assert _points("RX 7600\n💰 R$ 1.799,00\n🎟 com cupom X10 e pagando no pix!") == [
        ("pix", D("1799.00"), None, None, "", "stated_cue_apart")
    ]


def test_cue_apart_is_not_used_when_there_are_two_prices():
    points = _points("RX 7600\n💰 R$ 1.799\n💰 R$ 1.899\nPix")
    assert {p[0] for p in points} == {"unspecified"}


def test_lone_interest_free_line_adds_installment_option():
    assert _points("RTX 5050\n💵 R$ 2.115\n⭐ 10x sem juros") == [
        ("unspecified", D("2115.00"), None, None, "", "stated"),
        ("installments", D("2115.00"), 10, None, "free", "price_with_terms"),
    ]


def test_installments_in_parentheses_and_from_price():
    assert _points("💰 R$ 1.899 (10x sem juros)") == [
        ("installments", D("1899.00"), 10, None, "free", "stated")
    ]
    assert _points("a partir de R$ 1.699 no pix")[0][:2] == ("pix", D("1699.00"))


def test_original_not_kept_when_not_higher():
    assert parse_terms("De R$ 1.500 por R$ 1.599").original_price is None
