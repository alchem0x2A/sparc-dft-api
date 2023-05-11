import numpy as np
import os
from pathlib import Path
from ase.calculators.calculator import Calculator, FileIOCalculator, all_changes
from ase.units import Bohr, Hartree, fs, GPa
from ase.calculators.calculator import CalculatorError, CalculatorSetupError
from ase.calculators.calculator import CalculationFailed, SCFError, ReadError
from ase.calculators.singlepoint import SinglePointCalculator
from ase.calculators.calculator import compare_atoms
from ase.atoms import Atoms
import subprocess

from .sparc_io_bundle import SparcBundle
from .utils import _find_default_sparc, h2gpts
from warnings import warn

# Below are a list of ASE-compatible calculator input parameters that are
# in Angstrom/eV units
# Ideas are taken from GPAW calculator
sparc_python_inputs = ["xc", "h", "kpts", "convergence", "gpts", "nbands", "encut"]

from .inputs import SparcInputs

defaultAPI = SparcInputs()


class SPARC(SparcBundle, FileIOCalculator):
    # TODO: magmom should be a possible input
    implemented_properties = ["energy", "forces", "fermi", "stress"]
    name = "sparc"
    ase_objtype = "sparc_calculator"  # For JSON storage
    special_inputs = sparc_python_inputs

    def __init__(
        self,
        restart=None,
        directory=".",
        *,
        label=None,
        atoms=None,
        command=None,
        psp_dir=None,
        **kwargs,
    ):
        FileIOCalculator.__init__(
            self, restart=restart, label=label, atoms=atoms, command=command, **kwargs
        )

        self.directory = Path(directory)
        # TODO: change label?
        self.label = label if label is not None else "SPARC"
        self.sparc_bundle = SparcBundle(
            directory=self.directory,
            mode="w",
            atoms=atoms,
            label=self.label,
            psp_dir=psp_dir,
        )
        # Run a short test to return version of SPARC's binary
        self.sparc_version = self._detect_sparc_version()
        # Sanitize the kwargs by converting lower -- > upper
        # and perform type check
        self.valid_params, self.special_params = self._sanitize_kwargs(kwargs)
        self.raw_results = {}
        
    @property
    def sort(self):
        """Like Vasp calculator
        ASE atoms --> sort --> SPARC
        """
        return self.sparc_bundle.sorting["sort"]
    
    @property
    def resort(self):
        """Like Vasp calculator
        SPARC --> resort --> ASE atoms
        """
        return self.sparc_bundle.sorting["resort"]

    def _make_command(self, extras=""):
        """Use $ASE_SPARC_COMMAND or self.command to determine the command
        as a last resort, if `sparc` exists in the PATH, use that information

        Extras will add additional arguments to the self.command,
        e.g. -name, -socket etc


        """
        if isinstance(extras, (list, tuple)):
            extras = " ".join(extras)
        else:
            extras = extras.strip()
        if self.command is None:
            command_env = os.environ.get("ASE_SPARC_COMMAND", None)
            if command_env is None:
                sparc_exe, mpi_exe, num_cores = _find_default_sparc()
                if sparc_exe is None:
                    raise EnvironmentError(
                        "Cannot find your sparc setup via $ASE_SPARC_COMMAND, SPARC.command, or "
                        "infer from your $PATH. Please refer to the manual!"
                    )
                if mpi_exe is not None:
                    command_env = f"{mpi_exe} -n {num_cores} {sparc_exe}"
                else:
                    command_env = f"{sparc_exe}"
                warn(
                    f"Your sparc command is inferred to be {command_env}, "
                    "If this is not correct, please manually set $ASE_SPARC_COMMAND or SPARC.command!"
                )
            self.command = command_env
        return f"{self.command} {extras}"

    def calculate(self, atoms=None, properties=["energy"], system_changes=all_changes):
        Calculator.calculate(self, atoms, properties, system_changes)
        self.write_input(self.atoms, properties, system_changes)
        self.execute()
        self.read_results()

    def write_input(self, atoms, properties=None, system_changes=None):
        """Create input files via SparcBundle"""
        # import pdb; pdb.set_trace()
        FileIOCalculator.write_input(self, atoms, properties, system_changes)

        converted_params = self._convert_special_params(atoms=atoms)
        input_parameters = converted_params.copy()
        input_parameters.update(self.valid_params)
        # TODO: detect if minimal values are set

        self.sparc_bundle._write_ion_and_inpt(
            atoms=atoms,
            label=self.label,
            # Pass the rest parameters from calculator!
            direct=False,
            sort=True,
            ignore_constraints=False,
            wrap=False,
            # Below are the parameters from v1
            # scaled -> direct, ignore_constraints --> not add_constraints
            scaled=False,
            add_constraints=True,
            copy_psp=True,
            comment="",
            input_parameters=input_parameters,
        )

    def execute(self):
        """Make the calculation. Note we probably need to use a better handling of background process!"""
        # TODO: add -socket?
        extras = f"-name {self.label}"
        command = self._make_command(extras=extras)

        # TODO: distinguish between normal process
        try:
            self.proc = subprocess.run(command, shell=True, cwd=self.directory)
        except OSError as err:
            msg = 'Failed to execute "{}"'.format(command)
            raise EnvironmentError(msg) from err

        # We probably don't want to wait the
        errorcode = self.proc.returncode

        if errorcode > 0:
            msg = f"SPARC failed with command {command}" f"with error code {errorcode}"
            raise CalculationFailed(msg)

        return

    def read_results(self):
        """Parse from the SparcBundle"""
        raw_result_dict = self.sparc_bundle.read_results()
        self.raw_results = raw_result_dict
        # TODO: result result function should actually planted back to SparcBundle
        if "static" in self.raw_results:
            self._extract_static_results()
        elif "geopt" in self.raw_results:
            self._extract_geopt_results()
        elif "aimd" in self.raw_results():
            self._extract_aimd_results()
        else:
            # TODO: should be another error instead?
            raise CalculationFailed("Cannot read SPARC output!")
        # Result of the output results, currently only E-fermi
        self._extract_out_results()
        
    def _extract_static_results(self):
        """Extract energy / forces from static results
        """
        static_results = self.raw_results.get("static", {})
        if "free energy" in static_results:
            self.results["energy"] = static_results["free energy"]
            # TODO: shall we distinguish?
            self.results["free energy"] = static_results["free energy"]
        
        if "forces" in static_results:
            # The forces are already re-sorted!
            self.results["forces"] = static_results["forces"]
            
        if "stress" in static_results:
            self.results["stress"] = static_results["stress"]
        
        return
    
    def get_last_ionic_step(self):
        """Get last ionic step dict from raw results
        """
        out_results = self.raw_results.get("out", {})
        ionic_steps = out_results.get("ionic_steps", [])
        if len(ionic_steps) > 0:
            return ionic_steps[-1]
        else:
            return {}
        
    
    def _extract_out_results(self):
        """Extract extra information from results
        """
        last_step = self.get_last_ionic_step()
        if "fermi level" in last_step:
            value = last_step["fermi level"]["value"]
            unit = last_step["fermi level"]["unit"]
            if unit.lower() == "ev":
                self.results["fermi"] = value
            # Should rarely happen, but keep it here!
            elif unit.lower() == "hartree":
                self.results["fermi"] = value * Hartree
            else:
                raise ValueError("Wrong unit in Fermi!")
        return
    
    def get_fermi_level(self):
        """Extra get-method for Fermi level, if calculated
        """
        return self.results.get("fermi", None)
        
        
    def _detect_sparc_version(self):
        """Run a short sparc test to determine which sparc is used"""
        # TODO: complete the implementation
        return None

    def _sanitize_kwargs(self, kwargs):
        """Convert known parameters from"""
        print(kwargs)
        # TODO: versioned validator
        validator = defaultAPI
        valid_params = {}
        special_params = {}
        # SPARC API is case insensitive
        for key, value in kwargs.items():
            if key in self.special_inputs:
                special_params[key] = value
            else:
                key = key.upper()
                if key in valid_params:
                    warn(
                        f"Parameter {key} (case-insentivie) appears multiple times in the calculator setup!"
                    )
                if validator.validate_input(key, value):
                    valid_params[key] = value
                else:
                    # TODO: helper information
                    warn(f"Input parameter {key} does not have a valid value!")
        return valid_params, special_params

    def _convert_special_params(self, atoms=None):
        """Convert ASE-compatible parameters to SPARC compatible ones
        parameters like `h`, `nbands` may need atoms information
        """
        converted_sparc_params = {}
        validator = defaultAPI
        params = self.special_params.copy()

        # xc --> EXCHANGE_CORRELATION
        if "xc" in params:
            xc = params.pop("xc")
            if xc.lower() == "pbe":
                converted_sparc_params["EXCHANGE_CORRELATION"] = "GGA_PBE"
            elif xc.lower() == "lda":
                converted_sparc_params["EXCHANGE_CORRELATION"] = "LDA_PW"

        # h --> gpts
        if "h" in params:
            if "gpts" in params:
                raise KeyError(
                    "h and gpts cannot be provided together in SPARC calculator!"
                )
            h = params.pop("h")
            if atoms is None:
                raise ValueError(
                    "Must have an active atoms object to convert h --> gpts!"
                )
            # TODO: is there any limitation for parallelization?
            gpts = h2gpts(h, atoms.cell)
            params["gpts"] = gpts

        # gpts --> FD_GRID
        if "gpts" in params:
            gpts = params.pop("gpts")
            if validator.validate_input("FD_GRID", gpts):
                converted_sparc_params["FD_GRID"] = gpts
            else:
                # TODO: customize error
                raise ValueError(f"Input parameter gpts has invalid value {gpts}")

        # kpts
        if "kpts" in params:
            # TODO: how about accepting ASE's kpts setting?
            kpts = params.pop("kpts")
            if validator.validate_input("KPOINT_GRID", kpts):
                converted_sparc_params["KPOINT_GRID"] = kpts
            else:
                # TODO: customize error
                raise ValueError(f"Input parameter kpts has invalid value {kpts}")

        # nbands
        if "nbands" in params:
            # TODO: Check if the nbands are correct in current system
            # TODO: default $N_e/2 \\times 1.2 + 5$
            nbands = params.pop("nbands")
            if validator.validate_input("NSTATES", nbands):
                converted_sparc_params["NSTATES"] = nbands
            else:
                # TODO: customize error
                raise ValueError(f"Input parameter nbands has invalid value {nbands}")

        return converted_sparc_params
