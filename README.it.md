# Appliance Cycle

*[English](README.md)*

Integrazione per Home Assistant che trasforma un qualunque **sensore di
potenza** (presa smart, misuratore di energia, ...) in un monitor dei cicli di
lavastoviglie, lavatrice o asciugatrice:

- rileva **inizio** e **fine** del ciclo;
- **impara i programmi** dal profilo di consumo e li riconosce le volte
  successive;
- mostra il **tempo rimanente**, l'**ora di fine stimata** e l'**avanzamento**;
- invia una **notifica** alla fine del ciclo (e, se vuoi, qualche minuto prima).

Niente cloud né API dell'elettrodomestico: solo le letture di potenza già
presenti in Home Assistant.

## Installazione

### HACS

1. HACS → ⋮ → *Repository personalizzati* → aggiungi
   `https://github.com/scoprire87/washingmachine` come *Integrazione*.
2. Installa **Appliance Cycle** e riavvia Home Assistant.

### Manuale

Copia `custom_components/appliance_cycle` nella cartella `custom_components`
della configurazione e riavvia Home Assistant.

## Configurazione

*Impostazioni → Dispositivi e servizi → Aggiungi integrazione → Appliance Cycle*

| Campo | Descrizione |
| --- | --- |
| Nome | es. `Lavastoviglie` |
| Sensore di potenza | un `sensor` con classe *potenza* (W o kW) |
| Elettrodomestico | lavastoviglie, lavatrice, asciugatrice o altro: imposta soglie adatte |

Poi apri **Configura**. Il menu offre:

- **Notifiche e rilevamento** (sotto);
- **Rinomina un programma**, **Correggi il programma dell'ultimo ciclo**,
  **Elimina programmi** (uno o più insieme);
- **Durate dei programmi (display)**: un programma per riga con la durata
  indicata dal display, ad esempio `Intenso 3:16`. Quando il ciclo è
  riconosciuto come uno di questi programmi, il tempo rimanente è un conto alla
  rovescia da quella durata, come sul display. Il ciclo non viene considerato
  finito prima di quella durata meno 10 minuti (l'asciugatura di alcuni
  programmi, come l'Eco, resta a 0 W anche per un'ora) e il programma
  riconosciuto resta anche se la fine del ciclo non somiglia ai lavaggi salvati;
- **Carica i programmi preimpostati** (lavastoviglie): programmi registrati da
  una lavastoviglie reale, utili se hai lo stesso modello. I programmi imparati
  con un nome automatico che corrispondono a uno di questi ne prendono il nome;
- **Impara da un log CSV**: carica un CSV scaricato dalla cronologia del
  sensore di potenza (*Cronologia → ⋮ → Scarica dati*), anche dal telefono.

Notifiche e rilevamento:

| Opzione | Descrizione |
| --- | --- |
| Notifiche | uno o più servizi notify (`notify.mobile_app_...`, `notify.telegram`, `persistent_notification.create`, ...) |
| Avvisa prima della fine | promemoria N minuti prima della fine stimata (0 = disattivato) |
| Mantieni "terminato" per | minuti in cui lo stato resta *terminato* prima di tornare *inattivo* |
| Rilevamento (avanzate) | potenza a riposo, potenza di avvio, ritardo di fine |

Il sensore dovrebbe aggiornarsi almeno ogni 10–30 secondi mentre
l'elettrodomestico lavora; la maggior parte delle prese smart lo fa.

## Entità

| Entità | Descrizione |
| --- | --- |
| `binary_sensor.<nome>_running` | acceso durante il ciclo e mentre l'elettrodomestico assorbe potenza (`active`) |
| `sensor.<nome>_status` | `idle`, `active` (assorbe potenza ma il ciclo non è ancora confermato, es. prelavaggio), `running`, `finished`; attributi `power`, `started`, `last_cycle` |
| `sensor.<nome>_program` | programma riconosciuto; attributi `confidence`, `known_programs` |
| `sensor.<nome>_remaining_time` | minuti alla fine |
| `sensor.<nome>_estimated_end` | ora di fine stimata |
| `sensor.<nome>_progress` | % del ciclo |
| `sensor.<nome>_cycle_energy` | kWh del ciclo in corso / ultimo |

(Gli ID effettivi dipendono dalla lingua con cui viene creata l'integrazione.)

## Come funziona

**Rilevamento.** Il ciclo inizia quando la potenza supera la *potenza di
avvio* e finisce quando resta sotto la *potenza a riposo* per il *ritardo di
fine*. Le attività brevi che non raggiungono la potenza di avvio (display,
apertura automatica della porta) vengono ignorate.

Per la lavastoviglie la potenza a riposo predefinita è 1 W: dopo l'ultimo
riscaldamento il programma continua fino a ~2 ore con l'asciugatura (ventola
~15 W, poi letture che alternano 0 e 2 W) e finisce quando lo indica il
display. Con soglie più alte (es. 4 W) l'asciugatura resta fuori dal ciclo.

**Apprendimento.** Ogni ciclo terminato viene salvato come profilo di
consumo. Cicli con lo stesso profilo formano un programma (`Programma 1`,
`Programma 2`, ...), che puoi rinominare da **Configura**.

Quando dopo **30 minuti** dall'avvio il ciclo non corrisponde ancora a nessun
programma conosciuto arriva la domanda **«che programma è?»**. Si risponde con
il nome e il tempo che manca sul display, ad esempio `Eco 2:41` (oppure
`Eco 161`): il ciclo prende quel programma, la durata totale è il tempo già
trascorso più quello indicato, il tempo rimanente segue quella durata e il
ciclo viene salvato con quel nome. Se il programma non aveva ancora una durata
del display, gli viene assegnata. Basta anche solo il nome: in quel caso vale
la durata del display già salvata. Se il programma viene riconosciuto
prima, la domanda non parte. Un ciclo più corto di 30 minuti mai visto riceve
invece, a fine ciclo, la domanda **«che programma era?»**: sulle notifiche
dell'app (`notify.mobile_app_...`) c'è il pulsante *Rispondi*, basta scrivere
il nome (es. `intenso`, viene scritto come nelle durate del display). La
domanda compare anche nelle notifiche di Home Assistant e viene emesso
l'evento `appliance_cycle_unknown_program`.

Se il ciclo dura più dei lavaggi salvati (ad esempio lavaggi salvati prima di
includere l'asciugatura) ma la durata è entro il 15% di quella del display del
programma riconosciuto durante il ciclo, gli viene assegnato quel programma.

**Tempo rimanente.** Durante il ciclo, il profilo fin qui viene allineato
(dynamic time warping) con quelli appresi: il migliore indica il programma e
il punto in cui si trova. Finché il ciclo non somiglia a nessun programma
appreso (un programma mai usato prima), tempo rimanente e programma restano
*sconosciuti* invece di mostrare la stima di un altro programma; il nuovo
programma viene imparato alla fine del ciclo.

Provato su tracce reali di una lavastoviglie (9 programmi, leave-one-out): il
programma viene riconosciuto da circa metà ciclo e a metà ciclo la fine
stimata è tipicamente entro 2–4 minuti, entro 2 minuti nell'ultimo quarto.
Programmi che iniziano allo stesso modo (es. *Rapido* e *Completo*) possono
essere confusi nei primi minuti; la stima si corregge appena divergono.

## Partire più velocemente

Se il sensore di potenza è già registrato in Home Assistant, impara subito i
cicli passati:

```yaml
action: appliance_cycle.import_history
data:
  entity_id: sensor.lavastoviglie_stato
  days: 10
```

Se hai dei log esportati dallo storico (*Cronologia → ⋮ → Scarica dati*, un
CSV per programma), caricali da **Configura → Impara da un log CSV** indicando
il nome del programma, oppure copiali nella cartella di configurazione e usa il
servizio:

```yaml
action: appliance_cycle.import_file
data:
  entity_id: sensor.lavastoviglie_stato
  file: lavastoviglie/eco.csv
  program: Eco
```

Poi rinomina i programmi da **Configura → Rinomina un programma**, oppure con
il servizio:

```yaml
action: appliance_cycle.rename_program
data:
  entity_id: sensor.lavastoviglie_stato
  program: Programma 1
  new_name: Eco
```

## Servizi

| Servizio | Descrizione |
| --- | --- |
| `import_history` | impara i cicli presenti nello storico (`days`, predefinito 10); restituisce `{cycles: n}` |
| `import_file` | impara i cicli di un CSV esportato dallo storico (`file` relativo alla cartella di configurazione, `program` facoltativo); restituisce `{cycles: n}` |
| `rename_program` | rinomina un programma appreso |
| `delete_program` | dimentica un programma |
| `set_last_program` | l'ultimo ciclo è stato riconosciuto male: assegnalo a un altro programma (nuovo o esistente) |
| `set_program_durations` | durate del display per programma (`programs`: `{"Intenso": "3:16"}` o testo `Intenso 3:16` una riga per programma); restituisce `{programs: n}` |
| `archive_runs` | archivia i lavaggi salvati: servono ancora a riconoscere i programmi ma non contano più nelle durate medie quando ci sono lavaggi nuovi; restituisce `{runs: n}` |

## Eventi e automazioni

Sul bus vengono emessi `appliance_cycle_started`, `appliance_cycle_finished`
e `appliance_cycle_unknown_program` (ciclo di un programma mai visto: `running`
vale `true` se la domanda parte durante il lavaggio, con `started`; a fine ciclo
`program` è il nome automatico e `duration` è in secondi).
L'evento di fine contiene `name`, `program`, `started`, `ended`, `duration`
(s) ed `energy` (Wh).

```yaml
triggers:
  - trigger: state
    entity_id: sensor.lavastoviglie_stato
    to: finished
actions:
  - action: light.turn_on
    target:
      entity_id: light.cucina
    data:
      flash: short
```

## Suggerimenti

- Dopo un riavvio il ciclo in corso viene ripreso dallo storico: viene riletta la durata del programma più lungo (display o lavaggi salvati) più 30 minuti, 8 ore se non ci sono ancora programmi. Un ciclo già finito che inizia prima di questa finestra viene ignorato, non imparato come programma nuovo.
- Se i cicli finiscono troppo presto (pause lunghe, ammollo), aumenta il
  *ritardo di fine*.
- Se lo standby o il display fanno partire cicli, alza la *potenza di avvio*;
  se la fine non viene mai rilevata, alza la *potenza a riposo*.

## Licenza

MIT
