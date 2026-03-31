"""HA persistent notification manager for Petkit Smart Devices integration.

Fires a native Home Assistant persistent notification whenever a relevant
device event occurs (litter-box cleaning result, waste bin full, low food,
low water, etc.).  Each notification type is gated by the corresponding
notification switch already present on the device
(e.g. ``work_notify``, ``litter_full_notify``) so users can opt-out of
individual alerts directly from the integration's switch entities.

When a binary alert clears (e.g. the waste bin has been emptied) the
matching persistent notification is automatically dismissed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pypetkitapi import Feeder, Litter, WaterFountain

from homeassistant.components.persistent_notification import async_create, async_dismiss

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import PetkitDataUpdateCoordinator

LOGGER = logging.getLogger(__name__)


def _safe_get(obj: Any, *attrs: str, default: Any = None) -> Any:
    """Safely retrieve a chain of attributes without raising AttributeError."""
    try:
        for attr in attrs:
            obj = getattr(obj, attr)
        return obj
    except AttributeError:
        return default


def _device_name(device: Any) -> str:
    """Return a human-readable device name."""
    return (
        _safe_get(device, "device_nfo", "device_name")
        or _safe_get(device, "name")
        or f"PetKit device {device.id}"
    )


class PetkitNotificationManager:
    """Fires HA persistent notifications when PetKit device events occur.

    Instantiate once per config entry and pass in the main data coordinator.
    The manager registers itself as a coordinator listener so it is called
    automatically after every data refresh.  Call :meth:`stop` when the
    config entry is unloaded to remove the listener.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PetkitDataUpdateCoordinator,
    ) -> None:
        """Register as a coordinator listener."""
        self.hass = hass
        self._coordinator = coordinator
        self._prev_litter_events: dict[int, str | None] = {}
        self._prev_binary: dict[str, bool] = {}
        self._remove_listener = coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    def stop(self) -> None:
        """Unregister the coordinator listener (call on integration unload)."""
        self._remove_listener()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _notify(self, notification_id: str, title: str, message: str) -> None:
        async_create(
            self.hass,
            message=message,
            title=title,
            notification_id=notification_id,
        )

    def _dismiss(self, notification_id: str) -> None:
        async_dismiss(self.hass, notification_id)

    def _notif_enabled(self, device: Any, setting_attr: str | None) -> bool:
        """Return True when the device's notification switch is enabled (or always if None)."""
        if setting_attr is None:
            return True
        return bool(_safe_get(device, "settings", setting_attr, default=False))

    def _track_binary(
        self, device_id: int, key: str, current: Any
    ) -> tuple[bool, bool]:
        """Track a boolean state across updates.

        Returns ``(rose, fell)`` where *rose* means a False→True transition
        occurred and *fell* means a True→False transition occurred.
        """
        state_key = f"{device_id}_{key}"
        prev = self._prev_binary.get(state_key, False)
        current_bool = bool(current)
        self._prev_binary[state_key] = current_bool
        return current_bool and not prev, not current_bool and prev

    # ------------------------------------------------------------------
    # Per-device-type checks
    # ------------------------------------------------------------------

    def _check_litter(self, device: Litter) -> None:
        """Check litter box work events and binary alerts."""
        from .utils import map_litter_event

        # --- Litter box event (cleaning completed / failed, pet visit, etc.) ---
        current_event = map_litter_event(device.device_records)
        prev_event = self._prev_litter_events.get(device.id)
        if current_event and current_event != prev_event:
            self._prev_litter_events[device.id] = current_event
            if self._notif_enabled(device, "work_notify"):
                self._notify(
                    f"petkit_{device.id}_litter_event",
                    f"PetKit — {_device_name(device)}",
                    current_event,
                )

        # --- Waste bin full ---
        rose, fell = self._track_binary(
            device.id, "box_full", _safe_get(device, "state", "box_full")
        )
        if rose and self._notif_enabled(device, "litter_full_notify"):
            self._notify(
                f"petkit_{device.id}_box_full",
                f"PetKit — {_device_name(device)}",
                "The waste bin is full and needs to be emptied.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_box_full")

        # --- Sand / litter level low ---
        rose, fell = self._track_binary(
            device.id, "sand_lack", _safe_get(device, "state", "sand_lack")
        )
        if rose and self._notif_enabled(device, "lack_sand_notify"):
            self._notify(
                f"petkit_{device.id}_sand_lack",
                f"PetKit — {_device_name(device)}",
                "Litter level is low. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_sand_lack")

    def _check_feeder(self, device: Feeder) -> None:
        """Check feeder food-level alerts."""
        food_state = _safe_get(device, "state", "food")
        food_low = food_state is not None and food_state < 2
        rose, fell = self._track_binary(device.id, "food_low", food_low)
        if rose and self._notif_enabled(device, "food_notify"):
            self._notify(
                f"petkit_{device.id}_food_low",
                f"PetKit — {_device_name(device)}",
                "Food level is low. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_food_low")

    def _check_fountain(self, device: WaterFountain) -> None:
        """Check water fountain alerts."""
        # --- Water level low ---
        rose, fell = self._track_binary(
            device.id, "lack_warning", _safe_get(device, "lack_warning")
        )
        if rose and self._notif_enabled(device, "lack_liquid_notify"):
            self._notify(
                f"petkit_{device.id}_lack_warning",
                f"PetKit — {_device_name(device)}",
                "Water level is low. Please refill.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_lack_warning")

        # --- Filter needs replacement ---
        rose, fell = self._track_binary(
            device.id, "filter_warning", _safe_get(device, "filter_warning")
        )
        if rose:
            self._notify(
                f"petkit_{device.id}_filter_warning",
                f"PetKit — {_device_name(device)}",
                "The water filter needs to be replaced.",
            )
        elif fell:
            self._dismiss(f"petkit_{device.id}_filter_warning")

    # ------------------------------------------------------------------
    # Coordinator update handler
    # ------------------------------------------------------------------

    def _handle_coordinator_update(self) -> None:
        """Called by the coordinator after every successful data refresh."""
        if not self._coordinator.data:
            return
        for device in self._coordinator.data.values():
            try:
                if isinstance(device, Litter):
                    self._check_litter(device)
                elif isinstance(device, Feeder):
                    self._check_feeder(device)
                elif isinstance(device, WaterFountain):
                    self._check_fountain(device)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning(
                    "PetKit: error while processing notification for device %s: %s",
                    getattr(device, "id", "?"),
                    exc,
                )
