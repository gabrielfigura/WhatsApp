```python
import asyncio
import aiohttp
import logging
import json
import re
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler
from telegram.error import TelegramError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from collections import Counter, deque
import uuid
from datetime import datetime

# Configurações do Bot
BOT_TOKEN = "7758723414:AAF-Zq1QPoGy2IS-iK2Wh28PfexP0_mmHHc"  # Substitua pelo seu token
CHAT_ID = "-1002506692600"  # Substitua pelo seu chat ID
JSON_URL = f"https://www.elephantbet.co.ao/homepage.json?v={datetime.now().strftime('%m/%d/%Y-%H:%M')}"  # JSON público para Bac Bo Ao Vivo

# Inicializar o bot e a aplicação
bot = Bot(token=BOT_TOKEN)
application = Application.builder().token(BOT_TOKEN).build()

# Configuração de logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s")

# Histórico e estado
historico = deque(maxlen=50)  # Histórico de até 50 resultados (P, B, T)
empates_historico = []  # Armazena empates com scores
ultimo_padrao_id = None
ultimo_resultado_id = None
sinais_ativos = []
placar = {
    "ganhos_seguidos": 0,
    "ganhos_gale1": 0,
    "ganhos_gale2": 0,
    "losses": 0,
    "empates": 0
}
ultima_mensagem_monitoramento = None
detecao_pausada = False
aguardando_validacao = False

# Mapeamento de outcomes para emojis e letras
OUTCOME_MAP = {
    "PlayerWon": ("🔵", "P"),
    "BankerWon": ("🔴", "B"),
    "Tie": ("🟡", "T")
}

# Função para normalizar resultados do JSON
def normalize_token_to_label(s):
    if not isinstance(s, str):
        return None
    t = s.strip().lower()
    if re.search(r'\bplayer\b|^p[:\s]|azul|blue|\bpl\b', t):
        return 'P'
    if re.search(r'\bbanker\b|^b[:\s]|vermelh|red|\bbk\b', t):
        return 'B'
    if re.search(r'\btie\b|empate|draw|tie_game|^t\b', t):
        return 'T'
    return None

# Extrair histórico do JSON (foco em Bac Bo Ao Vivo)
def extract_history_from_json(data):
    results = []
    def search_keys(d):
        out = []
        if isinstance(d, dict):
            for k, v in d.items():
                lk = str(k).lower()
                if any(x in lk for x in ('bacbo', 'ao vivo', 'live', 'result', 'history')):
                    if isinstance(v, (dict, list)):
                        out.extend(extract_history_from_json(v))
                    else:
                        lab = normalize_token_to_label(str(v))
                        if lab:
                            out.append(lab)
                else:
                    if isinstance(v, (dict, list)):
                        out.extend(search_keys(v))
        elif isinstance(d, list):
            for item in d:
                if isinstance(item, (dict, list)):
                    out.extend(search_keys(item))
        return out
    try:
        res = search_keys(data)
        if res:
            results.extend(res)
    except:
        pass
    # Fallback regex
    if not results:
        js_text = json.dumps(data)
        for m in re.finditer(r'\b(Player|Banker|Tie|Empate|P:|B:|T:|\"P\"|\"B\"|\"T\")\b', js_text, re.IGNORECASE):
            lab = normalize_token_to_label(m.group(0))
            if lab:
                results.append(lab)
    # Dedupe consecutivos
    filtered = [r for r in results if r in ('P','B','T')]
    dedup = []
    for r in filtered:
        if not dedup or dedup[-1] != r:
            dedup.append(r)
    return dedup[-10:]  # Últimos 10 resultados

# Funções de estratégias (sem empate seco)
def oposto(c): return 'P' if c == 'B' else 'B'

def detectar_rampa(hist): return len(hist) >= 3 and hist[-1] == hist[-2] == hist[-3]

def detectar_rampa_invertida(hist): return len(hist) >= 3 and (hist[-3] == oposto(hist[-2]) == oposto(hist[-1]))

def detectar_barreira_de_4(hist): return len(hist) >= 4 and hist[-1] == hist[-2] == hist[-3] == hist[-4]

def detectar_padrao_3x2(hist): return len(hist) >= 5 and hist[-5] == hist[-4] and hist[-3] == hist[-2] and hist[-1] == hist[-3]

def detectar_parzinho(hist): return len(hist) >= 4 and hist[-4] == hist[-3] and hist[-2] == hist[-1]

def detectar_perninhas(hist): return len(hist) >= 6 and hist[-6] == hist[-4] == hist[-2]

def detectar_torres_gemeas(hist): return len(hist) >= 4 and hist[-4] == hist[-3] and hist[-2] == hist[-1] and hist[-4] == hist[-2]

def detectar_v(hist): return len(hist) >= 3 and hist[-3] == hist[-1] and hist[-2] != hist[-1]

def detectar_repeticao_quinta(hist): return len(hist) >= 5 and hist[-1] == hist[-5]

def detectar_quebra_surf(hist): return len(hist) >= 5 and hist[-4] == hist[-3] == hist[-2] and hist[-1] != hist[-2] and hist[-1] == hist[-4]

def estrategia_seq3(hist):
    if len(hist) >= 3 and hist[-1] == hist[-2] == hist[-3] and hist[-1] in ('P','B'):
        return ("Seq3", oposto(hist[-1]))
    return None

def estrategia_alternancia(hist):
    if len(hist) >= 4:
        last4 = hist[-4:]
        if last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1] and last4[0] in ('P','B'):
            return ("Alternancia", oposto(last4[-1]))
    return None

def estrategia_majority(hist, n=10):
    sample = [x for x in list(hist)[-n:] if x in ('P','B')]
    if len(sample) >= 4:
        cnt = Counter(sample)
        most, count = cnt.most_common(1)[0]
        if count > len(sample) / 2:
            return (f"Major_{n}", most)
    return None

def gerar_sinais_completos(hist):
    sinais = []
    if detectar_rampa(hist): sinais.append(("Rampa", oposto(hist[-1])))
    if detectar_rampa_invertida(hist): sinais.append(("Rampa Invertida", hist[-1]))
    if detectar_barreira_de_4(hist): sinais.append(("Barreira de 4", oposto(hist[-1])))
    if detectar_padrao_3x2(hist): sinais.append(("3x2", oposto(hist[-1])))
    if detectar_parzinho(hist): sinais.append(("Parzinho", oposto(hist[-1])))
    if detectar_perninhas(hist): sinais.append(("Perninhas", hist[-1]))
    if detectar_torres_gemeas(hist): sinais.append(("Torres Gêmeas", oposto(hist[-1])))
    if detectar_v(hist): sinais.append(("V", oposto(hist[-1])))
    if detectar_repeticao_quinta(hist): sinais.append(("Repetição na 5ª", hist[-1]))
    if detectar_quebra_surf(hist): sinais.append(("Quebra de Surf", hist[-1]))
    s = estrategia_seq3(hist)
    if s: sinais.append(s)
    s = estrategia_alternancia(hist)
    if s: sinais.append(s)
    s = estrategia_majority(hist, n=10)
    if s: sinais.append(s)
    seen = set()
    uniq = []
    for pad, sug in sinais:
        if sug not in seen:
            uniq.append((pad, sug))
            seen.add(sug)
    return uniq

@retry(stop=stop_after_attempt(7), wait=wait_exponential(multiplier=1, min=4, max=60), retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
async def fetch_resultado():
    """Busca resultados do JSON da Elephant Bet (foco em Bac Bo Ao Vivo)."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(JSON_URL, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    logging.debug("Resposta inválida do JSON: status %s", response.status)
                    return None, None, 0, 0, None
                data = await response.json()
                parsed = extract_history_from_json(data)
                if not parsed:
                    logging.debug("Nenhum resultado de Bac Bo encontrado no JSON")
                    return None, None, 0, 0, None
                # Último resultado como novo
                letra = parsed[-1] if parsed else None
                if not letra:
                    return None, None, 0, 0, None
                resultado = '🔵' if letra == 'P' else ('🔴' if letra == 'B' else '🟡')
                resultado_id = datetime.now().isoformat()  # Timestamp como ID
                player_score, banker_score = 0, 0  # JSON não inclui scores; use 0 como placeholder
                return resultado, resultado_id, player_score, banker_score, letra
        except Exception as e:
            logging.error("Erro ao buscar JSON: %s", e)
            return None, None, 0, 0, None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_sinal(sinal, padrao_id, resultado_id, sequencia, padrao_nome):
    global ultima_mensagem_monitoramento, aguardando_validacao
    try:
        if ultima_mensagem_monitoramento:
            try:
                await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
            except TelegramError:
                pass
            ultima_mensagem_monitoramento = None
        if aguardando_validacao or sinais_ativos:
            logging.info(f"Sinal bloqueado: aguardando validação ou sinal ativo (ID: {padrao_id})")
            return False
        sequencia_str = " ".join(sequencia)
        mensagem = f"""💡 CLEVER ANALISOU 💡
🧠 APOSTA EM: {sinal}
🛡️ Proteja o TIE 🟡
🤑 VAI ENTRAR DINHEIRO 🤑
⬇️ ENTRA NA COMUNIDADE DO WHATSAPP ⬇️"""
        keyboard = [
            [InlineKeyboardButton("Entrar no WhatsApp", url="https://chat.whatsapp.com/D61X4xCSDyk02srBHqBYXq")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem, reply_markup=reply_markup)
        sinais_ativos.append({
            "sinal": sinal,
            "letra": 'P' if sinal == '🔵' else ('B' if sinal == '🔴' else 'T'),
            "padrao_id": padrao_id,
            "padrao_nome": padrao_nome,
            "resultado_id": resultado_id,
            "sequencia": sequencia,
            "enviado_em": asyncio.get_event_loop().time(),
            "gale_nivel": 0,
            "gale_message_id": None
        })
        aguardando_validacao = True
        logging.info(f"Sinal enviado para padrão {padrao_nome} (ID: {padrao_id}): {sinal}")
        return message.message_id
    except TelegramError as e:
        logging.error(f"Erro ao enviar sinal: {e}")
        raise

async def mostrar_empates(update, context):
    """Handler para o botão EMPATES 🟡"""
    try:
        if not empates_historico:
            await update.callback_query.answer("Nenhum empate registrado ainda.")
            return
        empates_str = "\n".join([f"Empate {i+1}: 🟡 (🔵 {e['player_score']} x 🔴 {e['banker_score']})" for i, e in enumerate(empates_historico)])
        mensagem = f"📊 Histórico de Empates 🟡\n\n{empates_str}"
        await update.callback_query.message.reply_text(mensagem)
        await update.callback_query.answer()
    except TelegramError as e:
        logging.error(f"Erro ao mostrar empates: {e}")
        await update.callback_query.answer("Erro ao exibir empates.")

async def resetar_placar():
    global placar
    placar = {
        "ganhos_seguidos": 0,
        "ganhos_gale1": 0,
        "ganhos_gale2": 0,
        "losses": 0,
        "empates": 0
    }
    try:
        await bot.send_message(chat_id=CHAT_ID, text="🔄 Placar resetado após 10 erros! Começando do zero.")
        await enviar_placar()
    except TelegramError:
        pass

async def enviar_placar():
    try:
        total_acertos = placar['ganhos_seguidos'] + placar['ganhos_gale1'] + placar['ganhos_gale2'] + placar['empates']
        total_sinais = total_acertos + placar['losses']
        precisao = (total_acertos / total_sinais * 100) if total_sinais > 0 else 0.0
        precisao = min(precisao, 100.0)
        mensagem_placar = f"""🚀 CLEVER PERFORMANCE 🚀
✅SEM GALE: {placar['ganhos_seguidos']}
🔁GALE 1: {placar['ganhos_gale1']}
🔁GALE 2: {placar['ganhos_gale2']}
🟡EMPATES: {placar['empates']}
🎯ACERTOS: {total_acertos}
❌ERROS: {placar['losses']}
🔥PRECISÃO: {precisao:.2f}%"""
        await bot.send_message(chat_id=CHAT_ID, text=mensagem_placar)
    except TelegramError:
        pass

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_resultado(resultado, player_score, banker_score, resultado_id, letra):
    global rodadas_desde_erro, ultima_mensagem_monitoramento, detecao_pausada, placar, ultimo_padrao_id, aguardando_validacao, empates_historico
    try:
        if resultado == "🟡":
            empates_historico.append({"player_score": player_score, "banker_score": banker_score})
            if len(empates_historico) > 50:
                empates_historico.pop(0)
        for sinal_ativo in sinais_ativos[:]:
            if sinal_ativo["resultado_id"] != resultado_id:
                if resultado == sinal_ativo["sinal"] or resultado == "🟡":
                    if resultado == "🟡":
                        placar["empates"] += 1
                    if sinal_ativo["gale_nivel"] == 0:
                        placar["ganhos_seguidos"] += 1
                    elif sinal_ativo["gale_nivel"] == 1:
                        placar["ganhos_gale1"] += 1
                    else:
                        placar["ganhos_gale2"] += 1
                    if sinal_ativo["gale_message_id"]:
                        try:
                            await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                        except TelegramError:
                            pass
                    mensagem_validacao = f" 🤡ENTROU DINHEIRO🤡\n🎲 Resultado: 🔵 {player_score} x 🔴 {banker_score}"
                    await bot.send_message(chat_id=CHAT_ID, text=mensagem_validacao)
                    await enviar_placar()
                    ultimo_padrao_id = None
                    aguardando_validacao = False
                    sinais_ativos.remove(sinal_ativo)
                    detecao_pausada = False
                    logging.info(f"Sinal validado com sucesso para padrão {sinal_ativo['padrao_nome']} (ID: {sinal_ativo['padrao_id']})")
                else:
                    if sinal_ativo["gale_nivel"] == 0:
                        detecao_pausada = True
                        mensagem_gale = "🔄 Tentar 1º Gale"
                        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem_gale)
                        sinal_ativo["gale_nivel"] = 1
                        sinal_ativo["gale_message_id"] = message.message_id
                        sinal_ativo["resultado_id"] = resultado_id
                    elif sinal_ativo["gale_nivel"] == 1:
                        detecao_pausada = True
                        mensagem_gale = "🔄 Tentar 2º Gale"
                        try:
                            await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                        except TelegramError:
                            pass
                        message = await bot.send_message(chat_id=CHAT_ID, text=mensagem_gale)
                        sinal_ativo["gale_nivel"] = 2
                        sinal_ativo["gale_message_id"] = message.message_id
                        sinal_ativo["resultado_id"] = resultado_id
                    else:
                        placar["losses"] += 1
                        if sinal_ativo["gale_message_id"]:
                            try:
                                await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                            except TelegramError:
                                pass
                        await bot.send_message(chat_id=CHAT_ID, text="❌ NÃO FOI DESSA❌")
                        await enviar_placar()
                        if placar["losses"] >= 10:
                            await resetar_placar()
                        ultimo_padrao_id = None
                        aguardando_validacao = False
                        sinais_ativos.remove(sinal_ativo)
                        detecao_pausada = False
                        logging.info(f"Sinal perdido para padrão {sinal_ativo['padrao_nome']} (ID: {sinal_ativo['padrao_id']}), validação liberada")
                ultima_mensagem_monitoramento = None
            elif asyncio.get_event_loop().time() - sinal_ativo["enviado_em"] > 300:
                if sinal_ativo["gale_message_id"]:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=sinal_ativo["gale_message_id"])
                    except TelegramError:
                        pass
                ultimo_padrao_id = None
                aguardando_validacao = False
                sinais_ativos.remove(sinal_ativo)
                detecao_pausada = False
                logging.info(f"Sinal expirado para padrão {sinal_ativo['padrao_nome']} (ID: {sinal_ativo['padrao_id']}), validação liberada")
        if not sinais_ativos:
            aguardando_validacao = False
    except TelegramError:
        pass

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_monitoramento():
    global ultima_mensagem_monitoramento
    while True:
        try:
            if not sinais_ativos:
                if ultima_mensagem_monitoramento:
                    try:
                        await bot.delete_message(chat_id=CHAT_ID, message_id=ultima_mensagem_monitoramento)
                    except TelegramError:
                        pass
                message = await bot.send_message(chat_id=CHAT_ID, text="🔎MONITORANDO A MESA…")
                ultima_mensagem_monitoramento = message.message_id
            await asyncio.sleep(15)
        except TelegramError:
            await asyncio.sleep(15)

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(TelegramError))
async def enviar_relatorio():
    while True:
        try:
            total_acertos = placar['ganhos_seguidos'] + placar['ganhos_gale1'] + placar['ganhos_gale2'] + placar['empates']
            total_sinais = total_acertos + placar['losses']
            precisao = (total_acertos / total_sinais * 100) if total_sinais > 0 else 0.0
            precisao = min(precisao, 100.0)
            msg = f"""🚀 CLEVER PERFORMANCE 🚀
✅SEM GALE: {placar['ganhos_seguidos']}
🔁GALE 1: {placar['ganhos_gale1']}
🔁GALE 2: {placar['ganhos_gale2']}
🟡EMPATES: {placar['empates']}
🎯ACERTOS: {total_acertos}
❌ERROS: {placar['losses']}
🔥PRECISÃO: {precisao:.2f}%"""
            await bot.send_message(chat_id=CHAT_ID, text=msg)
        except TelegramError:
            pass
        await asyncio.sleep(3600)

async def enviar_erro_telegram(erro_msg):
    try:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ Erro detectado: {erro_msg}")
    except TelegramError:
        pass

async def main():
    global historico, ultimo_padrao_id, ultimo_resultado_id, detecao_pausada, aguardando_validacao
    application.add_handler(CallbackQueryHandler(mostrar_empates, pattern="mostrar_empates"))
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    asyncio.create_task(enviar_relatorio())
    asyncio.create_task(enviar_monitoramento())
    try:
        await bot.send_message(chat_id=CHAT_ID, text="🚀 Bot iniciado com sucesso!")
    except TelegramError:
        pass
    while True:
        try:
            resultado, resultado_id, player_score, banker_score, letra = await fetch_resultado()
            if not resultado or not resultado_id:
                await asyncio.sleep(2)
                continue
            if resultado_id == ultimo_resultado_id:
                await asyncio.sleep(2)
                continue
            ultimo_resultado_id = resultado_id
            historico.append(letra)
            if len(historico) > 50:
                historico.pop(0)
            await enviar_resultado(resultado, player_score, banker_score, resultado_id, letra)
            if not detecao_pausada and not aguardando_validacao and not sinais_ativos:
                sinais = gerar_sinais_completos(historico)
                if sinais:
                    padrao_nome, sugestao = sinais[0]
                    sinal_emoji = '🔵' if sugestao == 'P' else ('🔴' if sugestao == 'B' else '🟡')
                    padrao_id = str(uuid.uuid4())
                    sequencia = list(historico)[-5:] if len(historico) >= 5 else list(historico)
                    sequencia_emoji = ['🔵' if x == 'P' else ('🔴' if x == 'B' else '🟡') for x in sequencia]
                    enviado = await enviar_sinal(sinal_emoji, padrao_id, resultado_id, sequencia_emoji, padrao_nome)
                    if enviado:
                        ultimo_padrao_id = padrao_id
            await asyncio.sleep(2)
        except Exception as e:
            await enviar_erro_telegram(str(e))
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot encerrado pelo usuário")
    except Exception as e:
        logging.error(f"Erro fatal no bot: {e}")
        asyncio.run(enviar_erro_telegram(f"Erro fatal no bot: {e}"))
