"""
=============================================================
  LUAT — Backend Proxy para API Tiny v2
  Calculadora de Cubagem & Frete
=============================================================
  Uso local (porta 5050):
    1. Edite config.py com TINY_TOKEN
    2. Rode iniciar.bat

  Uso em nuvem (Render.com):
    - TINY_TOKEN como variável de ambiente no painel do Render
    - O HTML é servido embutido (sem arquivo externo)
=============================================================
"""

import os
import time
import logging
import webbrowser
import threading
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

# ── Token: variável de ambiente tem prioridade sobre config.py ─
TINY_TOKEN = os.environ.get("TINY_TOKEN", "")
if not TINY_TOKEN:
    try:
        from config import TINY_TOKEN
    except ImportError:
        pass

API_V2 = "https://api.tiny.com.br/api2"

app = Flask(__name__)
CORS(app)

# Caminho para o HTML (local: pasta acima do backend; nuvem: mesmo dir)
_base = Path(__file__).parent
HTML_PATH = _base.parent / "calculadora-cubagem-luat.html"
if not HTML_PATH.exists():
    HTML_PATH = _base / "calculadora-cubagem-luat.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def tiny_get_v2(endpoint, params=None):
    """Faz GET autenticado na API Tiny v2."""
    if not TINY_TOKEN:
        raise RuntimeError("Token Tiny não configurado. Edite o arquivo config.py (TINY_TOKEN).")
    p = {"token": TINY_TOKEN, "formato": "json"}
    if params:
        p.update(params)
    url = f"{API_V2}/{endpoint}"
    resp = requests.get(url, params=p, timeout=10)
    resp.raise_for_status()
    return resp.json()


def tiny_post_v2(endpoint, data=None):
    """Faz POST autenticado na API Tiny v2."""
    if not TINY_TOKEN:
        raise RuntimeError("Token Tiny não configurado. Edite o arquivo config.py (TINY_TOKEN).")
    d = {"token": TINY_TOKEN, "formato": "json"}
    if data:
        d.update(data)
    url = f"{API_V2}/{endpoint}"
    resp = requests.post(url, data=d, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Lógica: sugerir caixa ─────────────────────────────────
CAIXAS = [
    {"id": "pp", "nome": "PP",  "alt": 8,  "larg": 18, "comp": 12},
    {"id": "p",  "nome": "P",   "alt": 15, "larg": 23, "comp": 15},
    {"id": "m",  "nome": "M",   "alt": 30, "larg": 22, "comp": 23},
    {"id": "g",  "nome": "G",   "alt": 40, "larg": 30, "comp": 30},
    {"id": "gg", "nome": "GG",  "alt": 53, "larg": 45, "comp": 45},
]

def sugerir_caixa(alt_cm, larg_cm, comp_cm):
    """
    Retorna a menor caixa que comporta as dimensões do produto.
    Testa todas as orientações (rotações) do produto nas caixas.
    """
    dims_produto = sorted([alt_cm, larg_cm, comp_cm])

    for cx in CAIXAS:
        dims_caixa = sorted([cx["alt"], cx["larg"], cx["comp"]])
        if (dims_produto[0] <= dims_caixa[0] and
            dims_produto[1] <= dims_caixa[1] and
            dims_produto[2] <= dims_caixa[2]):
            return cx

    return {**CAIXAS[-1], "aviso": "Produto maior que GG — verificar embalagem especial"}

def calcular_volume_m3(alt, larg, comp):
    return round((alt / 100) * (larg / 100) * (comp / 100), 6)

def extrair_dimensoes(prod):
    """Extrai dimensões de um produto (compatível com API v2 e v3)."""
    alt  = float(prod.get("alturaEmbalagem")  or prod.get("altura")  or 0)
    larg = float(prod.get("larguraEmbalagem") or prod.get("largura") or 0)
    comp = float(prod.get("comprimentoEmbalagem") or prod.get("comprimento") or prod.get("profundidade") or 0)
    peso = float(prod.get("peso_bruto") or prod.get("pesoBruto") or prod.get("peso") or 0)
    return alt, larg, comp, peso


# ── Endpoints ─────────────────────────────────────────────

@app.route("/")
def index():
    """Serve a calculadora HTML diretamente via http://localhost:5050"""
    if HTML_PATH.exists():
        return send_file(str(HTML_PATH))
    return "<h2>Arquivo calculadora-cubagem-luat.html não encontrado.</h2><p>Certifique-se de que ele está em LUAT_Claude/ (uma pasta acima de cubagem-backend/).</p>", 404


@app.route("/callback")
def callback():
    return "<h3>✅ LUAT Cubagem — callback recebido.</h3><p><a href='/'>Voltar para a calculadora</a></p>"


@app.route("/health")
def health():
    ok = bool(TINY_TOKEN)
    return jsonify({"status": "ok" if ok else "sem_token", "server": "LUAT Cubagem Proxy", "version": "2.0", "api": "v2"})


@app.route("/produto")
def buscar_produto():
    """
    Busca produto no Tiny por código (SKU) ou nome.
    Retorna dimensões, peso e caixa sugerida.

    Query params:
      ?sku=CODIGO      → busca pelo código do produto
      ?nome=TEXTO      → busca pelo nome (retorna lista)
    """
    sku  = request.args.get("sku", "").strip()
    nome = request.args.get("nome", "").strip()

    if not sku and not nome:
        return jsonify({"erro": "Informe ?sku= ou ?nome="}), 400

    try:
        # Buscar produto(s) via API v2
        params = {"pesquisa": sku or nome}
        data = tiny_get_v2("produtos.pesquisa.php", params=params)

        retorno = data.get("retorno", {})
        if retorno.get("status") == "Erro":
            return jsonify({"erro": f"Produto não encontrado: {sku or nome}"}), 404

        itens = retorno.get("produtos", [])
        if not itens:
            return jsonify({"erro": f"Produto não encontrado: {sku or nome}"}), 404

        resultado = []
        for item in itens[:10]:
            prod_resumo = item.get("produto", item)
            prod_id = prod_resumo.get("id")

            # Buscar detalhes completos para ter as dimensões
            prod = prod_resumo
            if prod_id:
                try:
                    det = tiny_get_v2("produto.obter.php", params={"id": prod_id})
                    prod = det.get("retorno", {}).get("produto", prod_resumo)
                except Exception:
                    pass

            alt, larg, comp, peso = extrair_dimensoes(prod)
            tem_dimensoes = alt > 0 and larg > 0 and comp > 0
            caixa_sugerida = sugerir_caixa(alt, larg, comp) if tem_dimensoes else None
            volume = calcular_volume_m3(alt, larg, comp) if tem_dimensoes else None

            resultado.append({
                "id":            prod.get("id") or prod_id,
                "codigo":        prod.get("codigo") or prod.get("sku"),
                "nome":          prod.get("nome") or prod.get("descricao"),
                "unidade":       prod.get("unidade"),
                "alt_cm":        alt,
                "larg_cm":       larg,
                "comp_cm":       comp,
                "peso_kg":       peso,
                "volume_m3":     volume,
                "tem_dimensoes": tem_dimensoes,
                "caixa_sugerida": caixa_sugerida,
                "aviso": None if tem_dimensoes else "Dimensões não cadastradas no Tiny para este produto",
            })

        # Se buscou por SKU e achou 1, retorna objeto direto
        if sku and len(resultado) == 1:
            return jsonify(resultado[0])

        return jsonify({"resultados": resultado, "total": len(resultado)})

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else 500
        msg = {
            401: "Token inválido ou sem permissão. Verifique TINY_TOKEN em config.py.",
            429: "Rate limit da API Tiny atingido. Aguarde alguns segundos.",
            404: f"Produto não encontrado: {sku or nome}",
        }.get(status, str(e))
        return jsonify({"erro": msg}), status

    except RuntimeError as e:
        return jsonify({"erro": str(e)}), 503

    except Exception as e:
        log.exception("Erro inesperado em /produto")
        return jsonify({"erro": f"Erro interno: {str(e)}"}), 500


@app.route("/transportadoras")
def listar_transportadoras():
    """Retorna formas de envio cadastradas no Tiny."""
    try:
        data = tiny_get_v2("formas.envio.pesquisa.php")
        retorno = data.get("retorno", {})
        if retorno.get("status") == "Erro":
            return jsonify({"formas_envio": []})
        return jsonify(retorno)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/pedido")
def buscar_pedido():
    """
    Busca um pedido pelo número e retorna os itens com dimensões.

    Query params:
      ?numero=12345
    """
    numero = request.args.get("numero", "").strip()
    if not numero:
        return jsonify({"erro": "Informe ?numero= com o número do pedido"}), 400

    try:
        # Buscar pedido pelo número
        data = tiny_get_v2("pedidos.pesquisa.php", params={"numero": numero})
        retorno = data.get("retorno", {})

        if retorno.get("status") == "Erro":
            return jsonify({"erro": f"Pedido #{numero} não encontrado"}), 404

        pedidos = retorno.get("pedidos", [])
        if not pedidos:
            return jsonify({"erro": f"Pedido #{numero} não encontrado"}), 404

        pedido_resumo = pedidos[0].get("pedido", pedidos[0])
        pedido_id = pedido_resumo.get("id")

        # Buscar detalhes completos do pedido (com itens)
        det = tiny_get_v2("pedido.obter.php", params={"id": pedido_id})
        pedido = det.get("retorno", {}).get("pedido", pedido_resumo)
        itens_pedido = pedido.get("itens", [])

        itens_com_dimensoes = []
        for item in itens_pedido:
            prod_item = item.get("item", item)
            codigo  = prod_item.get("codigo") or prod_item.get("codigoProduto")
            nome    = prod_item.get("descricao") or prod_item.get("nome")
            qtd     = float(prod_item.get("quantidade") or 1)

            alt = larg = comp = peso = 0.0
            caixa_sug = None
            tem_dim = False
            aviso = None

            try:
                if codigo:
                    p = tiny_get_v2("produtos.pesquisa.php", params={"pesquisa": codigo})
                    ps = p.get("retorno", {}).get("produtos", [])
                    if ps:
                        prod_id = ps[0].get("produto", {}).get("id")
                        if prod_id:
                            pd = tiny_get_v2("produto.obter.php", params={"id": prod_id})
                            pr = pd.get("retorno", {}).get("produto", {})
                            alt, larg, comp, peso = extrair_dimensoes(pr)
                        if alt > 0 and larg > 0 and comp > 0:
                            tem_dim = True
                            caixa_sug = sugerir_caixa(alt, larg, comp)
                        else:
                            aviso = "Dimensões não cadastradas no Tiny"
            except Exception:
                aviso = "Falha ao buscar dimensões do produto"

            itens_com_dimensoes.append({
                "codigo":         codigo,
                "nome":           nome,
                "quantidade":     qtd,
                "alt_cm":         alt,
                "larg_cm":        larg,
                "comp_cm":        comp,
                "peso_kg":        peso,
                "volume_m3":      calcular_volume_m3(alt, larg, comp) if tem_dim else None,
                "tem_dimensoes":  tem_dim,
                "caixa_sugerida": caixa_sug,
                "aviso":          aviso,
            })

        return jsonify({
            "numero":      pedido.get("numero"),
            "id":          pedido.get("id"),
            "cliente":     pedido.get("nome") or pedido.get("cliente", {}).get("nome") if isinstance(pedido.get("cliente"), dict) else pedido.get("cliente"),
            "valor_total": pedido.get("totalPedido") or pedido.get("valor"),
            "situacao":    pedido.get("situacao"),
            "itens":       itens_com_dimensoes,
            "total_itens": len(itens_com_dimensoes),
        })

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else 500
        return jsonify({"erro": f"Erro HTTP {status}: {str(e)}"}), status
    except Exception as e:
        log.exception("Erro em /pedido")
        return jsonify({"erro": str(e)}), 500


# ── Main ───────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    log.info("=" * 55)
    log.info("  LUAT Cubagem Proxy v2.0 — porta %d", port)
    log.info("  Calculadora: http://localhost:%d", port)
    log.info("  Health: http://localhost:%d/health", port)
    log.info("=" * 55)

    if not TINY_TOKEN:
        log.warning("⚠  ATENÇÃO: TINY_TOKEN não configurado!")
        log.warning("   Edite cubagem-backend/config.py")

    def abrir_navegador():
        time.sleep(1.2)
        webbrowser.open(f"http://localhost:{port}")
    threading.Thread(target=abrir_navegador, daemon=True).start()

    app.run(host="127.0.0.1", port=port, debug=False)
