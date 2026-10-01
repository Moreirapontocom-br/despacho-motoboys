# Despacho de Motoboys

Sistema de despacho automático de entregas para restaurantes pequenos.
O restaurante cadastra os pedidos num painel, o sistema agrupa os que ficam perto
e escolhe o motoboy que vai conseguir entregar mais cedo. Cada motoboy vê a própria
rota no celular, com mapa, navegação pelo Google Maps e botão de "Entregue".

![Testes](https://github.com/Moreirapontocom-br/despacho-motoboys/actions/workflows/testes.yml/badge.svg)

## O que ele faz

**Painel do restaurante** (`/painel`)
- Cadastro de pedidos com busca de endereço e confirmação do ponto no mapa
- Número do pedido no dia (#1, #2...), fácil de falar no balcão e no telefone
- Despacho automático com um clique, ou atribuição manual a um motoboy
- Edição e cancelamento de pedidos
- Destaque para pedidos atrasados (na fila ou em rota)
- Botão de WhatsApp para avisar o cliente que o pedido saiu
- Resumo do dia (pendentes, em rota, entregues, tempo médio) e histórico em CSV
- Cadastro de motoboys e controle de turno

**Página do motoboy** (`/motoboy?id=NOME#codigo=CODIGO`)
- Rota do dia no mapa, na ordem certa
- Botões para navegar, ligar para o cliente e marcar como entregue
- Aviso na tela (e vibração) quando um pedido é cancelado
- Envio opcional da posição do GPS, para o despacho saber onde ele está

## Como o despacho escolhe o motoboy

Todo motoboy precisa passar no restaurante para pegar a comida. Por isso o sistema
não escolhe quem está mais perto do cliente, e sim **quem consegue terminar a entrega
mais cedo**: o tempo até ele voltar ao restaurante mais o tempo da nova viagem.

- Pedidos próximos (até 2 km entre si) são agrupados na mesma viagem.
- Se o motoboy ainda não saiu do restaurante, os pedidos novos entram na mesma
  viagem e a ordem das paradas é recalculada.
- Se ele já saiu, os novos ficam para depois que ele voltar.
- A ordem das paradas usa o tempo real pelas ruas (LocationIQ ou OSRM). Se o
  serviço estiver fora do ar, usa a distância em linha reta.

## Configuração (variáveis de ambiente)

Configure no painel do Render, em **Environment**. **Nunca coloque chaves no código.**

### Obrigatórias

| Variável | O que é |
|---|---|
| `API_KEY` | Senha do painel do restaurante. Use 32+ caracteres aleatórios. Gere com: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DATABASE_URL` | Endereço do banco Postgres (o Render cria ao adicionar um banco). Sem ela, o sistema usa um arquivo SQLite local, só para testes. |

### Restaurante

| Variável | Padrão | O que é |
|---|---|---|
| `RESTAURANTE_LAT` / `RESTAURANTE_LNG` | Itabira (MG) | Localização do restaurante |
| `RESTAURANTE_ENDERECO` | — | Endereço em texto, usado como ponto de partida no Google Maps |
| `RESTAURANTE_NOME` | — | Aparece na mensagem de WhatsApp para o cliente |
| `TIMEZONE_OFFSET_HORAS` | `-3` | Fuso horário em relação ao UTC |

### Mapas e rotas

| Variável | O que é |
|---|---|
| `LOCATIONIQ_KEY` | **Recomendada para uso comercial.** Sem ela, o sistema usa os serviços públicos gratuitos (Nominatim, OSRM e OpenStreetMap), que são só para testes. |
| `LOCATIONIQ_MAPA_KEY` | Segunda chave, só para as imagens do mapa (ela fica visível na página). Trave-a no painel da LocationIQ para funcionar só no endereço do seu site. |
| `OSRM_URL` | Servidor de rotas alternativo. Use `desligado` para usar só linha reta. |

### Ajustes do despacho (opcionais)

| Variável | Padrão | O que é |
|---|---|---|
| `PEDIDO_ATRASO_MIN` | `15` | Minutos na fila até o pedido aparecer como atrasado |
| `ROTA_ATRASO_MIN` | `40` | Minutos em rota até o pedido aparecer como atrasado |
| `PARADA_MIN` | `3` | Minutos gastos em cada entrega |
| `SAIDA_MIN` | `5` | Sem GPS, minutos após o despacho para considerar que o motoboy saiu |
| `MAX_PARADAS_VIAGEM` | `5` | Máximo de entregas numa mesma saída |
| `GPS_VALIDADE_MIN` | `10` | Por quantos minutos a posição do GPS vale |

## Publicar no Render

1. Crie um **Web Service** apontando para este repositório.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `uvicorn api_despacho3:app --host 0.0.0.0 --port $PORT`
4. Crie um banco **Postgres** no Render e copie o endereço para `DATABASE_URL`.
5. Configure `API_KEY` (e `LOCATIONIQ_KEY` para uso comercial).
6. Em **Settings > Auto-Deploy**, escolha **After CI Checks Pass**: o site só é
   atualizado quando os testes passam.

> O sistema roda em **um único worker**. As travas que impedem dois despachos ao
> mesmo tempo ficam na memória do servidor; com várias cópias rodando juntas, elas
> deixariam de funcionar.

## Rodar no computador

```bash
pip install -r requirements.txt
export API_KEY="uma-chave-qualquer-com-mais-de-24-caracteres"   # no Windows: set API_KEY=...
uvicorn api_despacho3:app --reload
```

Abra `http://127.0.0.1:8000/painel` e entre com a `API_KEY`.

## Testes

```bash
pip install -r requirements.txt httpx pytest
pytest -v
```

Os testes usam um banco SQLite temporário e desligam os serviços externos: os dados
reais nunca são tocados. Eles também rodam sozinhos no GitHub a cada envio de código
(aba **Actions**).

## Segurança

- O painel exige a `API_KEY` no cabeçalho `X-API-Key`.
- Cada motoboy tem um código próprio e só vê a própria rota. No banco fica só o
  hash do código (PBKDF2), nunca o código em si.
- Tentativas erradas bloqueiam por 15 minutos (8 por IP; 30 por nome de motoboy).
  Um motoboy que já entrou continua entrando mesmo que alguém tente trancar o nome dele.
- No link do motoboy, o código vai depois do `#`: essa parte não é enviada ao
  servidor nem aparece nos logs.
- A biblioteca do mapa é carregada com verificação de integridade (SRI).
