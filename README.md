# Watt Learner

A Home Assistant integration that learns in the background how many watts a device or a group of
devices draws, using nothing but the house meter.

## Idea

Home Assistant knows when a device switches on or off. Watt Learner compares the power at the house
meter shortly before and shortly after the switch: one minute before, and one minute after a short
settling time. A switch only counts when nothing else that Watt Learner watches switched close by
and the base load was steady. Over many clean measurements the median becomes the learned value.

One config entry is one device or one group, for example "TV living room" with the Apple TV, the
receiver and the TV set. The group counts as on as soon as one member is on.

## Setup

1. Copy `custom_components/watt_learner` into your Home Assistant `custom_components` folder (or add
   this repository to HACS as a custom repository) and restart Home Assistant.
2. Settings, Devices & services, Add integration, **Watt Learner**.
3. Enter a name, pick the house meter (a power sensor in W), optional compensation sensors and the
   members of the group (lights, switches, fans, media players, input booleans).
   Compensation sensors are added to the meter reading. Use them for anything that feeds into the
   house behind the meter, such as a balcony solar bank.
4. Each entry gets a device with three sensors and a button:
   - **Learned power**: the learned value in W, unknown until 5 measurements are there. Its attributes
     show the number of measurements, the spread, separate on/off values, a confidence level and the
     reasons for rejected switches.
   - **Power**: the learned value while the group is on, 0 while it is off.
   - **Measurements**: the number of stored measurements.
   - **Reset**: forgets everything learned for this entry.

The options change the meter, the compensation sensors, the members and the settling time after
switching (default 15 s; raise it for devices that take a while to reach their steady draw).

No switches are counted for two minutes after Home Assistant starts or the entry is loaded.

## Limits

- The meter in mind reports every 10 seconds, so every window is about a minute long and a device
  has to stay on for well over a minute to be measured.
- Below about 10 W there is nothing to measure against the noise of a household base load.
- Loads that vary on their own (fridges, heat pumps, a PC under load, a TV with changing brightness)
  give a wide spread; the median still helps, the confidence shows how far to trust it.
- Only switches of entities Watt Learner watches are known. Anything else that switches at the same
  time (an oven, a kettle) spoils that measurement; with enough measurements the median sorts it out.
- A group is measured as a whole. Members that switch separately within about a minute of each other
  count as one switch of the group, or are rejected as flapping.

## Example: Powercalc

Use the learned value in [Powercalc](https://github.com/bramstroker/homeassistant-powercalc) with a
fixed power template:

```yaml
powercalc:
  sensors:
    - entity_id: media_player.tv
      name: TV living room
      fixed:
        power: "{{ states('sensor.tv_living_room_learned_power') | float(0) }}"
```
