import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "robot"))
import barrido as b

def test_semilla_dedupe():
    # Simulate a key loaded from seed
    claves = {
        b.clave_vacante("Director Agropecuario (Bogotá)", "Empresa", "Bogota"): "2026-10-09"
    }
    
    # New item with slightly different title but should map to same deduplication logic
    k_nuevo = b.clave_vacante("Director/a Agropecuario (Ing. Agrónomo)", "Empresa", "Bogota")
    assert b.clave_repetida(k_nuevo, claves)

def test_empresa_compatible():
    # "tratar como la misma empresa si una contiene a la otra tras quitar sufijos..."
    k1 = b.clave_vacante("Ingeniero Agronomo", "Netafim (cliente externo)")
    k2 = b.clave_vacante("Ingeniero Agronomo", "Netafim SAS")
    claves = {k1: "2026-10-09"}
    assert b.clave_repetida(k2, claves)

def test_agencias():
    # "y Manpower... cuando el nombre real aparece en el cargo o la descripción"
    k1 = b.clave_vacante("Director Agrícola Netafim", "Adecco Colombia")
    k2 = b.clave_vacante("Director Agrícola Netafim", "Netafim")
    claves = {k1: "2026-10-09"}
    assert b.clave_repetida(k2, claves)

def test_rol_salario():
    # "Para cargos con palabra de rol... y empresa confidencial, comparar además ciudad y salario"
    k1 = b.clave_vacante("Gerente Agricola", "Confidencial", "Cali", "$10.000.000")
    k2 = b.clave_vacante("Gerente Agricola", "Empresa Confidencial", "Cali", "$12.000.000")
    claves = {k1: "2026-10-09"}
    # Should NOT be repeated because salary differs
    assert not b.clave_repetida(k2, claves)
    
    k3 = b.clave_vacante("Gerente Agricola", "Confidencial", "Cali", "$10.000.000")
    assert b.clave_repetida(k3, claves)
