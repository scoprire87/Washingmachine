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

Poi da **Configura** scegli:

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
| `binary_sensor.<nome>_running` | acceso durante il ciclo |
| `sensor.<nome>_status` | `idle`, `running`, `finished`; attributi `started`, `last_cycle` |
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
apertura automatica della porta, ventola dopo la fine) vengono ignorate.

**Apprendimento.** Ogni ciclo terminato viene salvato come profilo di
consumo. Cicli con lo stesso profilo formano un programma (`Programma 1`,
`Programma 2`, ...), che puoi rinominare con il servizio `rename_program`.

**Tempo rimanente.** Durante il ciclo, il profilo fin qui viene allineato
(dynamic time warping) con quelli appresi: il migliore indica il programma e
il punto in cui si trova. Finché nessun programma è stato appreso il tempo
rimanente non è disponibile.

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

Poi guarda `known_programs` sul sensore programma e rinominali:

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
| `rename_program` | rinomina un programma appreso |
| `delete_program` | dimentica un programma |
| `set_last_program` | l'ultimo ciclo è stato riconosciuto male: assegnalo a un altro programma (nuovo o esistente) |

## Eventi e automazioni

Sul bus vengono emessi `appliance_cycle_started` e `appliance_cycle_finished`.
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

- Dopo un riavvio il ciclo in corso viene ripreso dallo storico (ultime 8 ore).
- Se i cicli finiscono troppo presto (pause lunghe, ammollo), aumenta il
  *ritardo di fine*.
- Se lo standby o il display fanno partire cicli, alza la *potenza di avvio*;
  se la fine non viene mai rilevata, alza la *potenza a riposo*.

## Licenza

MIT
