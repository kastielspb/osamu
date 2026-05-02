# Pico-MMU: Руководство по сборке и запуску

## Содержание

1. [Необходимые компоненты](#1-необходимые-компоненты)
2. [Сборка узла Сателлита (Slave Box)](#2-сборка-узла-сателлита-slave-box)
3. [Сборка узла Оркестратора (Master)](#3-сборка-узла-оркестратора-master)
4. [Межблочная шина (RJ45)](#4-межблочная-шина-rj45)
5. [Прошивка Slave (MicroPython)](#5-прошивка-slave-micropython)
6. [Прошивка Master (Klipper MCU)](#6-прошивка-master-klipper-mcu)
7. [Установка Klipper extras модуля](#7-установка-klipper-extras-модуля)
8. [Конфигурация printer.cfg](#8-конфигурация-printercfg)
9. [Первый запуск и проверка](#9-первый-запуск-и-проверка)
10. [Устранение неисправностей](#10-устранение-неисправностей)

---

## 1. Необходимые компоненты

### На один Slave Box (4 катушки)

| Компонент | Кол-во | Примечание |
|-----------|--------|------------|
| Raspberry Pi Pico 2 (RP2350) | 1 | Или Pico 2 W (если нужен WiFi debug) |
| TMC2209 v3.1 (stepstick модуль) | 4 | Версия с UART-пинами (PDN/DIAG) |
| Шаговый двигатель Nema 14 | 4 | 35 мм, 200 шагов/оборот, ~0.8А. Альтернатива: Nema 17 pancake |
| MAX485 / SP485 модуль | 1 | TTL ↔ RS485 трансивер |
| Микропереключатель (микрик) | 4 | С роликом, NO/NC, для детекции филамента |
| WS2812B LED лента | 16 LED | Можно отрезать 16 диодов (4 на слот) |
| DC-DC понижающий (24V → 5V) | 1 | Минимум 3A (для Pico + LED). MP1584 или LM2596 |
| RJ45 розетка (female jack) | 2 | Panel mount. IN и OUT |
| USB-C панельный удлинитель | 1 | Panel mount extension для прошивки |
| Пневмофитинг PC4-M6 | 4+1 | 4 входа + 1 выход сплиттера |
| Сплиттер 4-в-1 (печатный) | 1 | Нейлон/SLA, углы каналов ≤20° |
| PTFE трубка 2×4 мм | ~2 м | От катушек до сплиттера |
| Конденсатор 100 мкФ 35V | 4 | По одному у каждого TMC2209 (VMOT) |
| Конденсатор 100 нФ керамический | 4 | Рядом с TMC2209 (декаплинг логики) |
| Резистор 1 кОм | 1 | Подтяжка TMC UART (между TX и RX) |

### На Master (Оркестратор)

| Компонент | Кол-во | Примечание |
|-----------|--------|------------|
| Raspberry Pi Pico 2 (RP2350) | 1 | |
| MAX485 / SP485 модуль | 1 | |
| Микропереключатель | 1 | Финальный датчик на выходе сплиттера |
| Сплиттер 4-в-1 (печатный) | 1 | Сводит трубки от slave-боксов |
| RJ45 розетка | 1 | Выход к первому slave |
| USB-C кабель | 1 | К хосту Klipper (Raspberry Pi) |
| Пневмофитинг PC4-M10 | 4+1 | Входы + выход к экструдеру |

### Общее

| Компонент | Кол-во | Примечание |
|-----------|--------|------------|
| Блок питания 24V ≥10A | 1 | Или отвод от БП принтера |
| Кабель Ethernet Cat5e (патчкорд) | По числу slave | Стандартные прямые (T568B-T568B) |
| PTFE трубка 2×4 мм | ~1м на slave | От slave-box до Master |

---

## 2. Сборка узла Сателлита (Slave Box)

### 2.1 Схема подключения Pico 2 (Slave)

```
                    ┌─────────────────────────────────┐
                    │       Raspberry Pi Pico 2       │
                    │            (RP2350)              │
                    │                                 │
          RS485     │  GP0 (UART0 TX) ──→ MAX485 DI  │
         трансивер  │  GP1 (UART0 RX) ←── MAX485 RO  │
                    │  GP2 (GPIO OUT) ──→ MAX485 DE+RE│
                    │                                 │
          TMC UART  │  GP4 (UART1 TX) ──→ TMC PDN    │──┐ 1кОм
                    │  GP5 (UART1 RX) ←──────────────│──┘ (между TX/RX)
                    │                                 │
         Stepper 0  │  GP6  ──→ TMC0 STEP            │
                    │  GP7  ──→ TMC0 DIR             │
                    │  GP14 ──→ TMC0 EN              │
                    │                                 │
         Stepper 1  │  GP8  ──→ TMC1 STEP            │
                    │  GP9  ──→ TMC1 DIR             │
                    │  GP15 ──→ TMC1 EN              │
                    │                                 │
         Stepper 2  │  GP10 ──→ TMC2 STEP            │
                    │  GP11 ──→ TMC2 DIR             │
                    │  GP16 ──→ TMC2 EN              │
                    │                                 │
         Stepper 3  │  GP12 ──→ TMC3 STEP            │
                    │  GP13 ──→ TMC3 DIR             │
                    │  GP17 ──→ TMC3 EN              │
                    │                                 │
         Sensors    │  GP18 ←── Микрик 0 (+ GND)     │
                    │  GP19 ←── Микрик 1 (+ GND)     │
                    │  GP20 ←── Микрик 2 (+ GND)     │
                    │  GP21 ←── Микрик 3 (+ GND)     │
                    │                                 │
         LED        │  GP22 ──→ WS2812B DIN          │
                    │                                 │
         Power      │  VSYS ←── 5V (от DC-DC)        │
                    │  GND  ←── GND                  │
                    └─────────────────────────────────┘
```

### 2.2 Подключение MAX485 (RS485 трансивер)

```
MAX485 модуль          Pico 2            Шина RJ45
─────────────          ──────            ─────────
VCC ←───────────────── 3V3               
GND ←───────────────── GND ──────────── Pin 3,6 (GND)
DI  ←───────────────── GP0 (TX)
RO  ────────────────→  GP1 (RX)
DE  ←───────────────── GP2               
RE  ←───────────────── GP2  (DE и RE соединены!)
A   ─────────────────────────────────── Pin 4 (RS485 A)
B   ─────────────────────────────────── Pin 5 (RS485 B)
```

> **Важно:** DE и RE пины MAX485 соединяются вместе и управляются одним GPIO. HIGH = передача, LOW = приём.

### 2.3 Подключение TMC2209 (Stepstick модули)

Каждый TMC2209 stepstick подключается одинаково, различаются только пины STEP/DIR/EN и адрес (MS1/MS2).

```
TMC2209 Stepstick      Подключение
────────────────       ───────────
VMOT ←─────────────── +24V (от шины RJ45, pin 1,2)
GND  ←─────────────── GND
VIO  ←─────────────── 3V3 (от Pico 3V3)
STEP ←─────────────── GP6/GP8/GP10/GP12 (по слоту)
DIR  ←─────────────── GP7/GP9/GP11/GP13 (по слоту)
EN   ←─────────────── GP14/GP15/GP16/GP17 (по слоту)
PDN/UART ←──────────── GP4 (общий UART TX, через 1 кОм)
           ──────────→ GP5 (UART RX, через тот же 1 кОм)

MS1, MS2 ── задают UART-адрес:
  TMC0: MS1=GND,  MS2=GND   → addr 0
  TMC1: MS1=3V3,  MS2=GND   → addr 1
  TMC2: MS1=GND,  MS2=3V3   → addr 2
  TMC3: MS1=3V3,  MS2=3V3   → addr 3

DIAG ── не подключать (или на отдельный GPIO для будущего StallGuard аппаратного IRQ)
```

**Схема подключения TMC UART (single-wire, shared):**

```
GP4 (TX) ───[1кОм]───┬──── TMC0 PDN
                      ├──── TMC1 PDN
                      ├──── TMC2 PDN
                      └──── TMC3 PDN
                      │
GP5 (RX) ─────────────┘
```

> Резистор 1 кОм между TX и шиной PDN. Все 4 TMC подключены к одной линии — адресация через MS1/MS2.

### 2.4 Подключение микропереключателей

```
Микрик (NO)    Подключение
───────────    ───────────
C (Common)  →  GND
NO (Normal Open) →  GP18/19/20/21

Внутренний PULL_UP включен программно (config: Pin.PULL_UP).
При нажатии (филамент давит) контакт замыкается → GP → LOW → filament detected.
```

**Размещение:** Каждый микрик стоит на входе в сплиттер, до точки сведения. Филамент давит на рычаг при прохождении.

### 2.5 Подключение WS2812B

```
WS2812B лента     Подключение
──────────────    ───────────
VCC (5V)  ←────── 5V (от DC-DC)
GND       ←────── GND
DIN       ←────── GP22
```

> Первые 4 LED = слот 0, следующие 4 = слот 1, и т.д. Отрезать точно 16 LED.

### 2.6 Питание Slave Box

```
RJ45 Pin 1,2 (+24V) ──→ VMOT всех TMC2209 (через 100мкФ конденсатор на каждый)
                    ──→ Вход DC-DC (24V → 5V)

DC-DC 5V выход ──→ Pico VSYS
               ──→ WS2812B VCC

RJ45 Pin 3,6 (GND) ──→ Общая земля всего бокса
```

> **Конденсаторы:** 100 мкФ электролитический + 100 нФ керамический рядом с каждым TMC2209 между VMOT и GND. Обязательно — иначе драйверы будут перегреваться или сбрасываться.

### 2.7 RJ45 разъёмы (Daisy Chain)

Два RJ45 jack соединены параллельно (все одноимённые пины спаяны):

```
RJ45 IN (от мастера/предыдущего slave)     RJ45 OUT (к следующему slave)
Pin 1 ──────────────────────────────────── Pin 1  (+24V)
Pin 2 ──────────────────────────────────── Pin 2  (+24V)
Pin 3 ──────────────────────────────────── Pin 3  (GND)
Pin 4 ──────────────────────────────────── Pin 4  (RS485 A)
Pin 5 ──────────────────────────────────── Pin 5  (RS485 B)
Pin 6 ──────────────────────────────────── Pin 6  (GND)
Pin 7 ──────────────────────────────────── Pin 7  (GND/+24V)
Pin 8 ──────────────────────────────────── Pin 8  (GND/+24V)
```

---

## 3. Сборка узла Оркестратора (Master)

### 3.1 Схема подключения Pico 2 (Master)

```
                    ┌─────────────────────────────────┐
                    │       Raspberry Pi Pico 2       │
                    │        (Master, RP2350)          │
                    │                                 │
          RS485     │  GP0 (UART0 TX) ──→ MAX485 DI  │
         трансивер  │  GP1 (UART0 RX) ←── MAX485 RO  │
                    │  GP2 (GPIO OUT) ──→ MAX485 DE+RE│
                    │                                 │
         Sensor     │  GP3 (GPIO IN)  ←── Микрик     │
                    │                  (+ GND)        │
                    │                                 │
         USB-C      │  USB ────────────→ Хост Klipper │
                    │                                 │
         Power      │  VBUS ←── 5V (от USB хоста)    │
                    │  GND  ←── GND                  │
                    └─────────────────────────────────┘
```

Master значительно проще Slave — только RS485 мост и один датчик.

### 3.2 Финальный микропереключатель

Устанавливается на выходе сплиттера Оркестратора, перед PTFE-трубкой идущей в экструдер K1.

```
Микрик ──→ GP3 (PULL_UP включён в firmware, active LOW)
```

При подаче филамента из любого slave → пруток доходит до Master сплиттера → давит на микрик → Klipper получает событие "filament present".

---

## 4. Межблочная шина (RJ45)

### 4.1 Распиновка (повтор из spec.md, T568B)

| Пин | Цвет (T568B) | Назначение |
|-----|--------------|------------|
| 1 | Оранжевый/белый | +24V |
| 2 | Оранжевый | +24V |
| 3 | Зелёный/белый | GND |
| 4 | Синий | RS485 A (+) |
| 5 | Синий/белый | RS485 B (−) |
| 6 | Зелёный | GND |
| 7 | Коричневый/белый | +24V (компенсация) |
| 8 | Коричневый | GND (компенсация) |

### 4.2 Кабели

Используются **стандартные прямые (straight-through) патчкорды Cat5e**. Длина до 5 м на сегмент — без проблем на 115200 baud.

> **Не используйте crossover кабели!** Только прямые (T568B на обоих концах).

### 4.3 Терминирование RS485

На последнем устройстве в цепочке (последний slave, порт OUT не подключён) рекомендуется установить **терминирующий резистор 120 Ом** между пинами 4 и 5 (A и B). Для коротких шин (< 3 м суммарно) можно обойтись без него.

### 4.4 Ограничения по мощности

- Ethernet Cat5e: сечение жилы ~0.2 мм² (AWG24)
- Допустимый ток на пару: ~1.5A
- Пины 1+2 (+24V) = до 3A, Пины 7+8 (дополнительные) = ещё до 3A
- Итого: **до 6A на 24V через один кабель** (144W)
- 4 мотора Nema14 × 0.8A = 3.2A пиково → один кабель Cat5e достаточно на один box

> Если используете Nema 17 с токами >1A, протяните отдельный силовой кабель или используйте два параллельных патчкорда.

---

## 5. Прошивка Slave (MicroPython)

### 5.1 Установка MicroPython на Pico 2

1. Скачайте MicroPython для RP2350:
   ```
   https://micropython.org/download/RPI_PICO2/
   ```
   Файл: `RPI_PICO2-xxxxxxxx-vX.X.X.uf2`

2. Подключите Pico 2 к компьютеру удерживая кнопку **BOOTSEL** — он появится как USB-накопитель.

3. Скопируйте `.uf2` файл на накопитель:
   ```bash
   cp RPI_PICO2-*.uf2 /media/$USER/RPI-RP2/
   ```
   Pico перезагрузится автоматически.

4. Проверьте подключение:
   ```bash
   # Должен появиться serial порт
   ls /dev/ttyACM*
   ```

### 5.2 Загрузка прошивки Slave

Используйте `mpremote` (рекомендуется) или Thonny IDE.

**Установка mpremote:**
```bash
pip install mpremote
```

**Загрузка файлов:**
```bash
cd /path/to/klipper_ams/slave

# Подключитесь к Pico
mpremote connect /dev/ttyACM0

# Загрузите все файлы рекурсивно
mpremote fs cp -r . :

# Или файл за файлом:
mpremote fs mkdir :bus
mpremote fs mkdir :motor
mpremote fs mkdir :peripheral

mpremote fs cp config.py :config.py
mpremote fs cp main.py :main.py
mpremote fs cp state_machine.py :state_machine.py

mpremote fs cp bus/__init__.py :bus/__init__.py
mpremote fs cp bus/protocol.py :bus/protocol.py
mpremote fs cp bus/rs485.py :bus/rs485.py

mpremote fs cp motor/__init__.py :motor/__init__.py
mpremote fs cp motor/tmc2209.py :motor/tmc2209.py
mpremote fs cp motor/stepper.py :motor/stepper.py
mpremote fs cp motor/slot.py :motor/slot.py

mpremote fs cp peripheral/__init__.py :peripheral/__init__.py
mpremote fs cp peripheral/sensor.py :peripheral/sensor.py
mpremote fs cp peripheral/led.py :peripheral/led.py
```

**Перезагрузка:**
```bash
mpremote reset
```

### 5.3 Проверка работоспособности

```bash
mpremote connect /dev/ttyACM0 repl
```

В REPL:
```python
from machine import Pin, UART
from bus.protocol import build_frame, CMD_PING, crc16_modbus

# Проверка CRC
print(hex(crc16_modbus(b'123456789')))  # Должно быть 0x4b37

# Проверка датчика (GP18)
sensor = Pin(18, Pin.IN, Pin.PULL_UP)
print(f"Sensor 0: {'triggered' if sensor.value() == 0 else 'open'}")

# Проверка LED
from peripheral.led import LEDStrip
leds = LEDStrip()
leds.set_slot(0, 1, 0, 255, 0)  # Слот 0 = зелёный
leds.update()
```

### 5.4 Обновление прошивки через Panel Mount USB

После сборки корпуса, Slave обновляется через вынесенный USB-C разъём:
```bash
mpremote connect /dev/ttyACM0 fs cp config.py :config.py
mpremote reset
```

> Не нужно разбирать бокс для обновления!

---

## 6. Прошивка Master (Klipper MCU)

### 6.1 Подготовка Klipper firmware для Master

Master использует стандартную прошивку Klipper MCU с добавленным модулем `pico_mmu`.

**Шаг 1: Клонировать Klipper (если ещё нет):**
```bash
cd ~
git clone https://github.com/Klipper3d/klipper.git
cd klipper
```

**Шаг 2: Скопировать модуль pico_mmu:**
```bash
cp -r /path/to/klipper_ams/master/pico_mmu/ ~/klipper/src/pico_mmu/
```

**Шаг 3: Добавить модуль в систему сборки Klipper:**

Отредактировать `~/klipper/src/Makefile` (или создать `Kconfig` запись):

```makefile
# В src/Makefile, добавить в секцию src-y:
src-$(CONFIG_MACH_RP2040) += pico_mmu/protocol.c pico_mmu/rs485.c pico_mmu/command_bridge.c
```

> **Примечание:** В текущей реализации platform-abstraction функции в `rs485.c` используют заглушки. Для полноценной работы нужно заменить их на вызовы Klipper API (`gpio_out_setup`, `gpio_out_write`, Klipper UART functions). Это интеграционная работа на этапе финальной сборки.

**Шаг 4: Конфигурация make menuconfig:**
```bash
cd ~/klipper
make menuconfig
```

Выбрать:
```
Micro-controller Architecture: Raspberry Pi RP2040/RP2350
Processor model: RP2350
Bootloader offset: No bootloader
Communication interface: USB
```

**Шаг 5: Сборка:**
```bash
make clean
make
```

Результат: `~/klipper/out/klipper.uf2`

**Шаг 6: Прошивка Master Pico 2:**
```bash
# Подключить Pico 2 (Master) удерживая BOOTSEL
cp ~/klipper/out/klipper.uf2 /media/$USER/RPI-RP2/
```

### 6.2 Проверка Master MCU в Klipper

После прошивки, Master должен определиться как serial устройство:
```bash
ls /dev/serial/by-id/usb-Klipper_rp2350_*
```

Типичный путь: `/dev/serial/by-id/usb-Klipper_rp2350_XXXXXXXXXXXXXXXX-if00`

---

## 7. Установка Klipper extras модуля

### 7.1 Копирование extras

```bash
cp /path/to/klipper_ams/extras/pico_mmu.py ~/klipper/klippy/extras/pico_mmu.py
```

### 7.2 Перезапуск Klipper

```bash
sudo systemctl restart klipper
```

### 7.3 Проверка загрузки модуля

В логах Klipper (`/tmp/klippy.log` или через Mainsail/Fluidd console):
```
Loaded MCU 'pico_mmu' ...
```

---

## 8. Конфигурация printer.cfg

Добавить в `printer.cfg`:

```ini
# ============================================================
# Pico-MMU Configuration
# ============================================================

[mcu pico_mmu]
serial: /dev/serial/by-id/usb-Klipper_rp2350_XXXXXXXXXXXXXXXX-if00
# Заменить XXXXX на реальный ID (из ls /dev/serial/by-id/)

[pico_mmu]
serial: pico_mmu
rs485_baud: 115200
tool_count: 8
polling_interval: 50

# Финальный датчик филамента на Master
[filament_switch_sensor mmu_sensor]
switch_pin: pico_mmu:gpio3
pause_on_runout: False
# Runout обрабатывается extras модулем, не стандартным механизмом

# --- Slave Boxes ---

[pico_mmu_slave box_0]
# unique_id определяется при первом MMU_HOME (см. логи)
unique_id: 0000000000000000
slots: 0,1,2,3

[pico_mmu_slave box_1]
unique_id: 0000000000000000
slots: 4,5,6,7

# --- Tool Change Macros ---

[gcode_macro T0]
gcode:
    MMU_CHANGE_TOOL TOOL=0

[gcode_macro T1]
gcode:
    MMU_CHANGE_TOOL TOOL=1

[gcode_macro T2]
gcode:
    MMU_CHANGE_TOOL TOOL=2

[gcode_macro T3]
gcode:
    MMU_CHANGE_TOOL TOOL=3

[gcode_macro T4]
gcode:
    MMU_CHANGE_TOOL TOOL=4

[gcode_macro T5]
gcode:
    MMU_CHANGE_TOOL TOOL=5

[gcode_macro T6]
gcode:
    MMU_CHANGE_TOOL TOOL=6

[gcode_macro T7]
gcode:
    MMU_CHANGE_TOOL TOOL=7
```

---

## 9. Первый запуск и проверка

### 9.1 Последовательность включения

1. **Подключить питание 24V** к шине (через RJ45 или отдельный разъём)
2. **Подключить Master USB** к хосту Klipper
3. **Запустить Klipper** — дождаться "Ready"
4. **Проверить связь с Master MCU:**
   ```gcode
   FIRMWARE_RESTART
   ```
   В логах должно быть: `MCU 'pico_mmu' configured`

### 9.2 Enumeration (обнаружение slave)

```gcode
MMU_HOME
```

**Ожидаемый вывод в консоли:**
```
MMU: Starting enumeration...
MMU: Slave 0123456789abcdef assigned addr 1
MMU: Home complete. 1 slaves online.
```

> Запишите `unique_id` из лога в `printer.cfg` для секции `[pico_mmu_slave]`!

### 9.3 Проверка статуса

```gcode
MMU_STATUS
```

**Ожидаемый вывод:**
```
MMU State: idle, Current tool: T-1
  Box 0 [ONLINE] addr=1:
    T0: LOADED
    T1: EMPTY
    T2: LOADED
    T3: EMPTY
```

### 9.4 Тестовая загрузка

```gcode
# Убедитесь что филамент заправлен в слот 0
MMU_LOAD TOOL=0
```

Должно произойти:
1. LED слота 0 → синий (пульсирует)
2. Мотор слота 0 крутится → филамент движется к Master
3. Микрик Master срабатывает → мотор переходит в Assist mode
4. LED → циан (горит ровно)

### 9.5 Тестовая выгрузка

```gcode
MMU_UNLOAD
```

### 9.6 Полная смена инструмента

```gcode
T0
# Ждём загрузку...
T1
# Ретракт T0, загрузка T1
```

---

## 10. Устранение неисправностей

### Slave не отвечает на DISCOVER

| Проверить | Как |
|-----------|-----|
| Питание 24V на шине | Мультиметр на пинах 1-2 RJ45 |
| 5V на Pico (DC-DC работает) | LED Pico горит? Мультиметр на VSYS |
| RS485 A/B правильно подключены | A→пин 4, B→пин 5, не перепутаны |
| DE/RE на GP2 | Осциллограф: при ответе GP2 должен кратко → HIGH |
| MicroPython загружен | Подключиться USB, проверить REPL |
| main.py запускается | В REPL: `import main` — не должно быть ошибок |

### TMC2209 не инициализируется

| Проверить | Как |
|-----------|-----|
| VMOT 24V присутствует | Мультиметр на пинах VMOT-GND TMC модуля |
| VIO 3.3V присутствует | От Pico 3V3 |
| MS1/MS2 правильно | Адреса 0-3 не дублируются |
| PDN/UART подключён через 1кОм | Прямое подключение без резистора → не работает |
| Конденсаторы установлены | 100мкФ + 100нФ у каждого TMC |

**Тест UART TMC в REPL:**
```python
from motor.tmc2209 import TMC2209Bank
bank = TMC2209Bank()
# Попытка чтения версии (должен вернуть число, не None)
drv = bank.drivers[0]
print(drv._read_reg(0x01))  # GSTAT register
```

### Мотор не крутится

| Проверить | Как |
|-----------|-----|
| EN pin LOW (активен) | `Pin(14).value()` → должен быть 0 |
| STEP пульсы идут | Осциллограф на GP6 |
| Обмотки подключены | 4 провода мотора: A1, A2, B1, B2 |
| Ток достаточный | Увеличить `DEFAULT_RUN_CURRENT_MA` |

**Быстрый тест мотора:**
```python
from motor.stepper import Stepper
s = Stepper(0)
s.enable()
s.start(200, forward=True)  # 200 Hz = медленное вращение
# Мотор должен крутиться
s.stop()
s.disable()
```

### Датчик не срабатывает

```python
from machine import Pin
p = Pin(18, Pin.IN, Pin.PULL_UP)
# Нажать микрик рукой
print(p.value())  # 0 = нажат, 1 = отпущен
```

Если всегда 0 — проверить, не замкнут ли провод на GND.
Если всегда 1 — проверить пайку, контакт микрика.

### RS485 шум / потеря пакетов

- Убедитесь в терминировании (120 Ом) на последнем устройстве
- Проверьте общую землю между всеми устройствами
- Уменьшите длину кабеля или снизьте baud rate до 57600
- Экранированный кабель (STP) вместо UTP при сильных помехах

### Watchdog срабатывает (все LED мигают красным)

Master перестал опрашивать slave >5 секунд. Причины:
- Klipper завис / перезагрузился
- USB кабель Master отключился
- RS485 шина оборвана

Решение: исправить связь, затем `MMU_HOME` для переинициализации.

---

## Приложение A: Инструменты для отладки

| Инструмент | Назначение |
|------------|------------|
| `mpremote` | Загрузка файлов, REPL доступ к Slave |
| Мультиметр | Проверка питания, continuity |
| Осциллограф / Logic Analyzer | RS485 сигналы, STEP пульсы, WS2812B тайминги |
| `picocom` / `minicom` | Прямой serial терминал к Pico |
| Klipper console (Mainsail) | G-code команды MMU_* |

## Приложение B: Рекомендуемые поставщики

| Компонент | Где купить |
|-----------|-----------|
| Pico 2 (RP2350) | raspberrypi.com, Aliexpress (WeAct Studio RP2350) |
| TMC2209 v3.1 stepstick | Aliexpress (BIGTREETECH TMC2209 V1.3) |
| MAX485 модуль | Aliexpress ("MAX485 TTL to RS485 module") |
| Nema 14 мотор | Aliexpress (35mm, 1.8°, 0.8A) |
| Микропереключатели | Aliexpress (KW12-3 roller lever) |
| Пневмофитинги PC4-M6 | Aliexpress ("PC4-M6 pneumatic fitting") |
| DC-DC MP1584 | Aliexpress ("MP1584 adjustable step-down") |
| RJ45 panel mount jack | Aliexpress ("RJ45 female panel mount") |
