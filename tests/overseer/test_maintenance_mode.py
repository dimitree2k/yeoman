"""Tests for maintenance mode manager."""
from __future__ import annotations
from yeoman_overseer.maintenance import MaintenanceManager

def test_no_maintenance_by_default() -> None:
    mm = MaintenanceManager()
    assert mm.is_active("yeoman-gateway") is False

def test_enter_and_check_maintenance() -> None:
    mm = MaintenanceManager()
    mm.enter("yeoman-gateway", timeout_s=120, reason="planned restart")
    assert mm.is_active("yeoman-gateway") is True

def test_maintenance_auto_expires() -> None:
    mm = MaintenanceManager()
    mm.enter("yeoman-gateway", timeout_s=0, reason="instant expire")
    assert mm.is_active("yeoman-gateway") is False

def test_exit_maintenance() -> None:
    mm = MaintenanceManager()
    mm.enter("yeoman-gateway", timeout_s=120, reason="planned restart")
    mm.exit("yeoman-gateway")
    assert mm.is_active("yeoman-gateway") is False

def test_get_active_maintenance() -> None:
    mm = MaintenanceManager()
    mm.enter("yeoman-gateway", timeout_s=120, reason="restart")
    mm.enter("yeoman-bridge", timeout_s=120, reason="update")
    active = mm.get_active()
    assert len(active) == 2

def test_export_import_state() -> None:
    mm = MaintenanceManager()
    mm.enter("yeoman-gateway", timeout_s=300, reason="restart")
    exported = mm.export_state()
    mm2 = MaintenanceManager()
    mm2.import_state(exported)
    assert mm2.is_active("yeoman-gateway") is True
