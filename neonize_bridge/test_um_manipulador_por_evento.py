"""O neonize guarda UM manipulador por tipo de evento (EventsManager.list_func):
registrar o mesmo tipo duas vezes apaga o primeiro em silencio. Este teste
impede que dois modulos da ponte registrem o mesmo evento."""
import collections
import pathlib
import re

AQUI = pathlib.Path(__file__).parent
REGISTRO = re.compile(r"cli\.event\(\s*nev\.([A-Za-z]+)\s*\)")


def registros():
    achados = collections.defaultdict(list)
    for arquivo in sorted(AQUI.glob("*.py")):
        if arquivo.name.startswith("test_"):
            continue
        for linha, texto in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1):
            achado = REGISTRO.search(texto)
            if achado:
                achados[achado.group(1)].append(f"{arquivo.name}:{linha}")
    return achados


def test_nenhum_tipo_de_evento_e_registrado_duas_vezes():
    repetidos = {tipo: onde for tipo, onde in registros().items() if len(onde) > 1}
    assert repetidos == {}, (
        "o neonize guarda um manipulador por tipo; o ultimo apaga o anterior: "
        f"{repetidos}")


def test_o_teste_enxerga_os_registros_que_existem():
    achados = registros()
    assert "MessageEv" in achados and "GroupInfoEv" in achados
    assert "JoinedGroupEv" in achados and "PictureEv" in achados
