import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from tgsearch import watch
from tgsearch.credentials import LiveConfigError
from tgsearch.models import Message


@pytest.mark.parametrize(
    ("model", "price", "expected"),
    [
        ("RX 7600 XT", "R$ 1.700,00 no Pix", "só Pix dentro do teto"),
        ("RX 7600", "R$ 1.700,00 no cartão", "cartão à vista"),
        ("RX 7600", "10x de R$ 170,00 sem juros", "cartão em 10×"),
        ("RX 7600", "R$ 1.700,01 no Pix", None),
        ("RX 7600 XT", "10x de R$ 170,01 sem juros", None),
        ("RTX 5050", "R$ 1.500,00 no Pix", None),
    ],
)
def test_ceiling_and_model(model, price, expected):
    message = Message("Offers", 1, None, f"Placa de vídeo {model}\n{price}")
    result = watch.match_offer(message, "channel")
    if expected:
        assert expected in result.body
    else:
        assert result is None


def test_card_first_and_pix_is_listed():
    message = Message(
        "Offers",
        1,
        None,
        "Placa de vídeo RX 7600\nR$ 1.500,00 no Pix\nR$ 1.650,00 no cartão",
    )
    result = watch.match_offer(message, "channel")
    assert result.priority == 0
    assert "cartão à vista" in result.body.splitlines()[0]
    assert "Pix" in result.body.splitlines()[1]


@pytest.mark.parametrize(
    ("offer", "price", "matches"),
    [
        ("Apple iPhone 17e de 256 GB", "R$ 3.700,00 no Pix", True),
        ("iPhone 17E 256GB Branco", "R$ 3.700,01 no Pix", False),
        ("iPhone 17 (256GB)", "10x de R$ 480,00 sem juros", True),
        ("iPhone 17 256GB", "10x de R$ 480,01 sem juros", False),
        ("iPhone 17 256GB", "R$ 4.699,00 no Pix", True),
        ("iPhone 17 Pro 256GB", "R$ 4.699,00 no Pix", False),
        ("iPhone 17e 512GB", "R$ 3.500,00 no Pix", False),
        ("iPhone 17e 256GB ou 512GB", "R$ 3.500,00 no Pix", False),
        ("iPhone 17e 256GB ou iPhone 17 256GB", "R$ 3.500,00 no Pix", False),
        ("iPhone 17 256GB seminovo", "R$ 4.600,00 no Pix", False),
        ("iPhone 17 256GB com cashback", "R$ 4.600,00 no Pix", False),
        ("iPhone 17 256GB", "R$ 4.800,00 no Pix\nFrete de R$ 40", False),
        ("Capinha para iPhone 17 256GB", "R$ 29,99 no Pix", False),
    ],
)
def test_phone_offer_limits(offer, price, matches):
    url = "https://www.mercadolivre.com.br/p/MLB123"
    message = Message("Offers", 2, None, f"{offer} lacrado\n{price}\n{url}")
    result = watch.match_offer(message, "channel")
    assert (result is not None) is matches
    if matches:
        assert url in result.body
        assert "Frete: até" in result.body


def test_phone_offer_needs_announced_seal_and_store_link():
    url = "https://www.mercadolivre.com.br/p/MLB123"
    text = "iPhone 17e 256 GB\nR$ 3.699,00 no Pix"
    assert watch.match_offer(Message("Offers", 2, None, f"{text}\n{url}"), "channel") is None
    assert watch.match_offer(Message("Offers", 2, None, f"lacrado {text}"), "channel") is None


def test_poll_cursor_dedupes_and_retries_delivery(monkeypatch):
    class Client:
        messages = [SimpleNamespace(id=10, message="old offer")]

        async def get_entity(self, chat):
            return SimpleNamespace(id=12, broadcast=True, username="offers")

        async def get_messages(self, entity, *, limit):
            return self.messages[-limit:]

        async def iter_messages(self, entity, *, min_id, reverse):
            assert reverse
            for item in self.messages:
                if item.id > min_id:
                    yield item

    client = Client()
    with sqlite3.connect(":memory:") as db:
        db.executescript(watch.SCHEMA)
        asyncio.run(watch.scan(client, db, ["@offers"]))
        assert not db.execute("SELECT * FROM outbox").fetchall()
        for item_id in (11, 12):
            client.messages.append(
                SimpleNamespace(
                    id=item_id,
                    message="Placa de vídeo Radeon RX 7600 XT\nR$ 1.700,00 no Pix",
                )
            )
        asyncio.run(watch.scan(client, db, ["@offers"]))
        db.commit()
        assert db.execute("SELECT last_id FROM cursors").fetchone() == (12,)
        (digest,) = db.execute("SELECT body FROM outbox").fetchone()
        assert digest.count("só Pix dentro do teto") == 1

        def fail(*args):
            raise LiveConfigError("offline")

        monkeypatch.setattr(watch, "send_summary", fail)
        with pytest.raises(LiveConfigError):
            watch.deliver(db, "token", "1")
        assert db.execute("SELECT body FROM outbox").fetchone() == (digest,)
        sent = []
        monkeypatch.setattr(watch, "send_summary", lambda token, chat, text: sent.append(text))
        watch.deliver(db, "token", "1")
        assert sent == [digest]
        asyncio.run(watch.scan(client, db, ["@offers"]))
        assert not db.execute("SELECT * FROM outbox").fetchall()
