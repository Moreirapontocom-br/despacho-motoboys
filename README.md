# Despacho de Motoboys

Sistema de despacho automático de entregas para restaurantes pequenos.
O restaurante cadastra os pedidos num painel, o sistema agrupa os que ficam perto
e escolhe o motoboy que vai conseguir entregar mais cedo. Cada motoboy vê a própria
rota no celular, com mapa, navegação pelo Google Maps e botão de "Entregue".

![Testes](https://github.com/Moreirapontocom-br/despacho-motoboys/actions/workflows/testes.yml/badge.svg)

## O que ele faz

**Painel do restaurante** (`/painel`)
- **Pedido rápido**: cole o endereço numa linha só (do WhatsApp, por exemplo) e aperte
  Enter. O painel separa rua, número, bairro, CEP e complemento sozinho, já mostra o
  ponto no mapa e outro Enter confirma. Os campos separados continuam em "Preencher
  campo por campo"
- Número do pedido no dia (#1, #2...), fácil de falar no balcão e no telefone
- **Três modos de despacho**, escolhidos no próprio painel:
  - **Manual**: um clique em "Despachar agora" distribui tudo (ou atribua um a um)
  - **Assistido**: o botão mostra a sugestão antes (quem leva o quê, km e minutos
    estimados, quantos km o agrupamento economiza e **por que** cada motoboy foi
    escolhido). O funcionário confirma ou troca o motoboy em "Escolher outro"
  - **Automático**: o sistema despacha sozinho a cada 20 s, seguindo as regras
- **Regras do despacho** (botão "Regras"): máximo de entregas por viagem, distância
  máxima para o automático mandar sozinho, quanto tempo um pedido sozinho espera um
  parceiro de viagem e depois de quanto tempo ele sai de qualquer jeito
- **Aguardando agrupamento**: pedido sozinho na região aparece em amarelo esperando;
  pedidos que podem ir juntos mostram "Pode ir junto com #12"
- Edição e cancelamento de pedidos
- **Mapa da operação**, a maior coluna do painel: pedidos aguardando (laranja),
  atrasados (vermelho piscando), pedidos em rota na cor de cada motoboy e motoboys com
  GPS. Embaixo, um cartão por motoboy (disponível, saindo, em rota ou voltando, com
  pedidos, km e quando fica livre); clicar nele mostra a rota e as paradas no mapa
- **Pedidos por situação**: 🟢 no prazo, 🟡 atenção (passou de 70% do limite) e
  🔴 atrasado, com faixa vermelha e contador no título da aba
- **Estatísticas** (hoje, 7 ou 30 dias): entregas, viagens, pedidos agrupados, km rodados
  e economizados, tempo médio, % no prazo, comparação com o período anterior e uma
  tabela por motoboy
- Botão de WhatsApp para avisar o cliente que o pedido saiu
- Resumo do dia (pendentes, em rota, entregues, tempo médio) e histórico em CSV
- Cadastro de motoboys só com nome, WhatsApp e tipo (próprio ou terceirizado): o código
  é gerado sozinho e o painel mostra o link, um QR Code e um botão para mandar o link
  no WhatsApp dele. Controle de turno
- Funciona em computador (até 3 colunas lado a lado), tablet e celular

**Página do motoboy** (`/motoboy?id=NOME#codigo=CODIGO`)
- Cartão grande com a **próxima entrega** e os botões principais
- Botão **"Saí do restaurante"**: a partir daí, pedidos novos ficam para a volta dele
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
| `RESTAURANTE_CIDADE` | `Itabira, MG` | Cidade que já vem preenchida no cadastro de pedidos |
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
| `PRAZO_ENTREGA_MIN` | `45` | Prazo total (pedido criado → entregue) usado no "% no prazo" das estatísticas |
| `FATOR_RUAS` | `1.3` | Os km das estatísticas são a linha reta vezes este número |
| `AUTOMATICO_INTERVALO_S` | `20` | No modo automático, de quantos em quantos segundos a fila é conferida |

O modo de despacho e as regras (máximo de entregas por viagem etc.) ficam no painel,
no botão **Regras**, e são guardados no banco. `MAX_PARADAS_VIAGEM` só vale como valor
inicial, até alguém salvar as regras.

## Estrutura do projeto

| Arquivo | O que tem |
|---|---|
| `api_despacho3.py` | Ponto de entrada (o Render inicia por aqui). Só junta as partes. |
| `config.py` | Todas as variáveis de ambiente, num lugar só |
| `horarios.py` | Horários e fuso. Regra: o banco guarda tudo em UTC |
| `banco.py` | Conexão, criação das tabelas e atualização de bancos antigos |
| `ajustes.py` | Modo de despacho e regras escolhidos no painel |
| `seguranca.py` | Chave do painel, códigos dos motoboys, limite de tentativas |
| `mapas.py` | Busca de endereço, distâncias e rotas pelas ruas |
| `despacho.py` | O algoritmo: agrupar pedidos, escolher motoboy, ordenar paradas |
| `api_painel.py` | Endereços usados pelo painel do restaurante |
| `api_motoboy.py` | Endereços usados pela página do motoboy |
| `paginas.py` | Carrega as páginas HTML com as configurações |
| `painel.html` | Página do restaurante |
| `motoboy.html` | Página do motoboy |
| `test_despacho.py` | Testes automáticos |

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
