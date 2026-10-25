"""Rotas de negocio: o que o cliente le de `commerce/account`."""
import sys
from pathlib import Path

import negocio

CLIENT = Path(__file__).resolve().parents[1] / "client"


def test_conta_sem_comercio_e_lida_pelo_cliente_como_sem_recursos_comerciais():
    sys.path.insert(0, str(CLIENT))
    try:
        from core import commerce
    finally:
        sys.path.remove(str(CLIENT))
    conta = commerce.parse_account(negocio.conta_sem_comercio())
    assert conta.business is False
    assert conta.cart_enabled is None
    assert conta.sellers == frozenset()


def test_cada_chamada_devolve_um_corpo_novo():
    negocio.conta_sem_comercio()["sellers"].append("x@c.us")
    assert negocio.conta_sem_comercio()["sellers"] == []
