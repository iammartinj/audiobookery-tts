# -*- coding: utf-8 -*-
"""Otevření souboru ve systému: na každé platformě tím, co tam existuje."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audiobookery as ab  # noqa: E402


CESTA = Path("/tmp/kniha")
# Ne literál: na Windows je z Path("/tmp/kniha") '\tmp\kniha'. Druh cesty se
# odvozuje z os.name při importu, patch sys.platform ho nezmění - porovnávat
# se proto musí s tím, co str(Path) vrátí na stroji, kde test běží.
OCEKAVANA = str(CESTA)


class OtevriVSystemu(unittest.TestCase):
    """Windows má os.startfile, zbytek světa systémový spouštěč."""

    def spust(self, platforma, spoustec_v_path=True):
        """Zavolá otevři na vymyšlené cestě a vrátí, co se z toho pustilo."""
        with mock.patch.object(ab.sys, "platform", platforma), \
             mock.patch.object(ab.os, "startfile", create=True) as startfile, \
             mock.patch.object(ab.shutil, "which",
                               return_value="/usr/bin/x" if spoustec_v_path else None), \
             mock.patch.object(ab.subprocess, "Popen") as popen:
            ab.otevri_v_systemu(CESTA)
        return startfile, popen

    def test_windows_pouzije_startfile(self):
        startfile, popen = self.spust("win32")
        startfile.assert_called_once_with(OCEKAVANA)
        popen.assert_not_called()

    def test_macos_pouzije_open(self):
        startfile, popen = self.spust("darwin")
        startfile.assert_not_called()
        self.assertEqual(popen.call_args.args[0], ["open", OCEKAVANA])

    def test_linux_pouzije_xdg_open(self):
        startfile, popen = self.spust("linux")
        startfile.assert_not_called()
        self.assertEqual(popen.call_args.args[0], ["xdg-open", OCEKAVANA])

    def test_spoustec_mimo_path_je_chyba(self):
        # Tiché nic by vypadalo jako že se složka neotevřela bez důvodu.
        with self.assertRaises(RuntimeError):
            self.spust("linux", spoustec_v_path=False)

    def test_spoustec_se_neceka(self):
        # Popen, ne run/call - xdg-open umí držet proces, dokud aplikace neskončí.
        _, popen = self.spust("darwin")
        self.assertTrue(popen.called)


if __name__ == "__main__":
    unittest.main()
