from datetime import datetime, timezone

import pytest

from tgsearch.gpu import brand_of, classify, mentions_target, models_in
from tgsearch.models import Message
from tgsearch.profiles import gpu_profile, offer_filter, terms_profile


@pytest.mark.parametrize(
    "text, models",
    [
        ("Placa de Vídeo RX 7600 8GB", ["rx7600"]),
        ("Radeon RX7600 Pulse", ["rx7600"]),
        ("rx-7600", ["rx7600"]),
        ("RX 7600 XT 16GB", ["rx7600xt"]),
        ("RX7600XT", ["rx7600xt"]),
        ("Processador Ryzen 5 7600", []),
        ("Ryzen 5 7600X", []),
        ("GeForce RTX 5050 Twin X2", ["rtx5050"]),
        ("RTX5050", ["rtx5050"]),
        ("RTX 50500", []),
        ("RTX 5060", []),
    ],
)
def test_models_in(text, models):
    assert models_in(text) == models


@pytest.mark.parametrize(
    "text, model, category, reason",
    [
        ("Placa de Vídeo Gigabyte Radeon RX 7600 Gaming OC 8GB GDDR6", "rx7600", "card", ""),
        ("NVIDIA GeForce RTX 5050 Twin X2, 8GB", "rtx5050", "card", ""),
        ("Na Rx7600 Triple Fan", "rx7600", "card", ""),
        ("Notebook ASUS V16 Core 5 RTX 5050 8GB", "rtx5050", "laptop", "category_laptop"),
        ("Notebook Lenovo LOQ-e RTX 5050 16GB", "rtx5050", "laptop", "category_laptop"),
        ("PC Gamer Ryzen 5 5500 Radeon RX 7600", "rx7600", "prebuilt", "category_prebuilt"),
        ("Combo Placa Mãe + RX 7600", "rx7600", "bundle", "category_bundle"),
        ("RX 7600 por R$ 1.799", "rx7600", "unknown", "category_unknown"),
        ("Placa de vídeo RX 7600 ou RTX 4060", "rx7600", "card", "other_gpu_named"),
        ("Placa de vídeo RX 7600 ou RX 7600 XT", "rx7600", "card", "other_gpu_named"),
        ("Placa de vídeo RTX 5050 e RX 7600", None, "card", "several_target_models"),
        ("Placa de vídeo RX 7600 XT", None, "card", "no_target_model"),
    ],
)
def test_classify(text, model, category, reason):
    result = classify(text)
    assert (result.model, result.category, result.reason) == (model, category, reason)


def test_xt_can_be_targeted_explicitly():
    assert classify("Placa de vídeo RX 7600 XT 16GB", ("rx7600xt",)).usable


def _m(text, urls=()):
    return Message("c", 1, datetime(2026, 11, 1, tzinfo=timezone.utc), text, tuple(urls))


def test_offer_filter_drops_chatter_outside_channels():
    accept = offer_filter(gpu_profile())
    priced = _m("RX 7600 por R$ 1.799")
    linked = _m("RX 7600 por R$ 1.799", urls=["https://kabum.com.br/x"])
    assert accept(priced, "channel")
    assert not accept(priced, "supergroup")  # no store link: chatter
    assert accept(linked, "supergroup")
    assert not accept(_m("alguém tem a RX 7600?"), "channel")
    assert not accept(_m("Ryzen 5 7600 R$ 899"), "channel")
    assert not accept(_m("RX 7600 R$ 1.799 https://t.me/grupo"), "supergroup")
    assert mentions_target("RTX 5050") and not mentions_target("RTX 5050", ("rx7600",))


def test_gpu_profile_queries_and_labels():
    p = gpu_profile(["rx7600"])
    assert p.queries == ("7600", "RX7600")
    assert p.label("rx7600") == "Radeon RX 7600"
    assert p.classify("Placa de vídeo RX 7600").usable


def test_terms_profile_for_any_product():
    p = terms_profile(["air fryer", "mondial"], exclude=["usada"])
    assert p.queries == ("air fryer",)  # AND: the longest term goes to Telegram
    assert p.classify("Air Fryer Mondial 4L R$ 299").usable
    assert p.classify("Air fryer Mondial usada").reason == "no_target_model"
    assert terms_profile(["ssd", "hd"], any_term=True).queries == ("ssd", "hd")
    accept = offer_filter(p)
    assert accept(_m("Air Fryer Mondial R$ 299"), "channel")
    assert not accept(_m("Air Fryer Mondial sem preço"), "channel")


def test_brand_of():
    assert brand_of("Placa de Vídeo RX 7600 Challenger ASRock 8GB") == "asrock"
    assert brand_of("Gigabyte AORUS RX 7600") == "gigabyte"
    assert brand_of("RX 7600 Power Color Fighter") == "powercolor"
    assert brand_of("RX 7600 ASRock ou XFX") == ""
