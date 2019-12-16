"""
This module provides the DataTaker class,
to process, validate and visualize data.

"""

import platform
from os.path import splitext, basename
from itertools import groupby
from tkinter import Tk
from tkinter.filedialog import askopenfilename, askopenfilenames
import re
import numpy as np
import pandas as pd
from pandas.plotting import register_matplotlib_converters
register_matplotlib_converters()
from math import floor, sqrt

from CoolProp.CoolProp import PropsSI as properties, PhaseSI as phase
from CoolProp.HumidAirProp import HAPropsSI as psychro
from cerberus import Validator

from ._plot import plot
from xpint import UnitRegistry
from vaplac import sauroneye

class DataTaker():
    """
    Process and visualize data from files generated by a data logger.

    A DataTaker object holds information about data contained in a file
    from a data logger (CSV or excel). The filename must be passed to
    the constructor. Moreover, a DataTaker has methods to validate
    or return the data. The latter can be useful to perform
    calculations that are not implemented in the DataTaker class.

    Because column names in data files may be quite long, it becomes
    tedious if the user has to type them numerous times.
    A DataTaker object thus links those names to alternate, shorter ones
    based on an external file that can be specified (default is
    `name_conversions_UTF8.txt` on unix-based systems, and
    `name_conversions_ANSI.txt` on windows sysems). This file also
    gives the units and property for each measured quantity.

    Parameters
    ----------
    filename : str
        The name of the DataTaker file (.csv or .xlsx) to read.

    Attributes
    ----------
    read_file : str
        The name of the data file that was read by the DataTaker.
    """

    ureg = UnitRegistry()
    Q_ = ureg.Quantity
    ureg.define('fraction = [] = frac = ratio')
    ureg.define('percent = 1e-2 frac = pct')
    ureg.define('ppm = 1e-6 fraction')

    def __init__(self, filenames=None, initialdir='.', filetype=None,
                 convert_file=None):
        self.raw_data = None
        self.read_files = []
        # assign read_file and raw_data attributes
        self.read(filenames, initialdir=initialdir)
        # assign _name_converter attribute
        if convert_file is None:
            encoding = 'ANSI' if platform.system() == 'Windows' else 'UTF8'
            convert_file = f'name_conversions_{encoding}.txt'
        self._build_name_converter(convert_file)
        self.quantities = {}
        self._groups = {}
        limits = np.array([-np.inf, 1, 30, 60, np.inf]) * self.ureg('min')
        self.set_steady_state_limits(limits)

    def __repr__(self):
        return f'DataTaker({self.read_files})'

    def _build_name_converter(self, filename):
        """
        Create a DataFrame to get the actual columns names
        in the DataTaker file.

        Parameters
        ----------
        filename : str, default 'name_conversions_UTF8.txt'
            The name of the DataTaker file.

        """

        # Read the label conversion table differently according to the OS
        nconv = pd.read_fwf(filename, comment='#',
                            widths=[12, 36, 20, 20, 5], index_col=0)
        nconv[nconv=='-'] = None
        self._name_converter = nconv

    def read(self, paths=None, initialdir='.', filetype=None):
        """
        Read a data file and assign it to the raw_data attribute.

        Parameters
        ----------
        paths : iterable of str or 'all', default None
            An iterable with the paths of the files to read. When set to
            'all', every file in initialdir is selected. If None
            is given, a dialog box will ask to select the files.
            Valid extensions are csv (.csv) and excel (.xlsx).
        initialdir : str, default '.'
            a string with the path of the directory in which the dialog
            box will open if no filename is specified.
        filetype : str, optional
            Extension of files to use for plotting (csv or excel).
            If not specified, files with both extension are used.
            Useful when `paths` is either 'all' or None.

        """

        if filetype is not None:
            filetype = filetype.lstrip('.').lower().replace('xlsx', 'excel')
        if paths is None:
            # Display default files based on the specified filetype
            if filetype is None:
                filetypes = (('All files', '.*'), ('CSV', '.csv'),
                             ('Excel', '.xlsx'))
            elif filetype.lower() in ('csv', '.csv'):
                filetypes = (('CSV', '.csv'), ('All files', '.*'))
            elif filetype.lower() in ('excel', 'xlsx', '.xlsx'):
                filetypes = (('Excel', '.xlsx'), ('All files', '.*'))
            Tk().withdraw()  # remove tk window
            # Open dialog window in initialdir
            paths = askopenfilenames(initialdir=initialdir,
                                     title='Select input file',
                                     filetypes=filetypes)
            # Return if the Cancel button is pressed
            if paths in ((), ''):
                return None
        elif paths == 'all':  # take every file in initialdir
            filenames = listdir(initialdir)
            if filetype is None:
                paths = [f'{initialdir}/{filename}' for filename in filenames]
            else:
                extension = filetype.replace('excel', 'xlsx')
                if extension.lstrip('.') not in ('csv', 'xlsx'):
                    raise ValueError('invalid file extension')
                paths = [f'{initialdir}/{filename}' for filename in filenames
                         if filename.lower().endswith(extension)]
        elif isinstance(paths, str):  # only one path given
            paths = [paths]

        def encoding(file):
            """Check the encoding of a file."""

            with open(file, encoding='UTF8') as f:
                try:
                    next(f)
                except UnicodeDecodeError:
                    return 'ISO-8859-1'
                else:
                    return 'UTF8'

        for i, path in enumerate(paths):
            _, extension = splitext(path.lower())
            if extension not in ('.csv', '.xlsx'):
                raise ValueError('invalid file extension')
            if filetype is None:
                filetype = {'.csv':'csv', '.xlsx':'excel'}[extension]
            # Define the reader function according to the file type
            call = 'read_' + filetype
            first_line = getattr(pd, call)(path,
                                           nrows=0, encoding=encoding(path))
            if any( word in list(first_line)[0] for word in
                ['load', 'aux', 'setpoint', '|', 'PdT'] ):
                # Print the test conditions if only one file
                if len(paths) == 1:
                    print('Test conditions :', list(first_line)[0])
                # Skip the first row containing the conditions
                raw_data = getattr(pd, call)(path, skiprows=1,
                                             encoding=encoding(path))
            else:
                raw_data = getattr(pd, call)(path, encoding=encoding(path))
            raw_data['Timestamp'] = pd.to_datetime(
                raw_data['Timestamp']
            ).apply(lambda t: t.round('s'))
            start_timestamp = raw_data['Timestamp'].iloc[0]
            stop_timestamp = raw_data['Timestamp'].iloc[-1]
            test_duration = stop_timestamp - start_timestamp
            start_time = start_timestamp.strftime('%d/%m %H:%M')
            stop_time = stop_timestamp.strftime('{}%H:%M'.format(
                '%d/%m ' if test_duration > pd.Timedelta('1 day') else '')
            )
            raw_data['file_index'] = i
            raw_data['test_period'] = f'{start_time} - {stop_time}'
            raw_data['test_duration'] = test_duration
            if self.raw_data is None:
                self.raw_data = raw_data
            else:
                self.raw_data = pd.concat([self.raw_data, raw_data],
                                          sort=False).reset_index(drop=True)
            self.read_files.append(basename(path))

    def get_timestep(self, kind='Timedelta'):
        """
        Return the duration between two consecutive data samples.

        Parameter
        ---------
        as : 'Timedelta' or 'Quantity', default 'Timedelta'
            Specifiy the type of the object returned.

        Returns
        -------
        Pandas Timedelta, or Quantity

        """

        t0 = self.raw_data['Timestamp'].iloc[0]
        t1 = self.raw_data['Timestamp'].iloc[1]
        if kind == 'Timedelta':
            return t1 - t0
        elif kind == 'Quantity':
            return (t1 - t0).seconds * self.ureg('seconds')
        else:
            raise ValueError("kind must be either 'Timedelta' or 'Quantity'.")

    def _build_quantities(self, *quantities, **kwargs):
        """
        Add quantities to the DataTaker's quantities attribute,
        optionally returning them as Quantity objects.

        Parameters
        ----------
        *quantities : {'T{1-9}', 'h{1-9}', 'Ts', 'Tr', 'Tin', 'Tout',
                       'Tamb', 'Tdtk', 'RHout','Tout_db', 'pin', 'pout',
                       'refdir', 'Pa', 'Pb', 'Pfan_out', 'f', 'Pfan_in',
                       'Ptot', 'Qcond', 'Qev', 'Pcomp', 'flowrt_r', 't'}

        Example
        -------
        >>> dtk = vpa.DataTaker()
        >>> quantities = ('T1', 'T2', 'T3')
        >>> T1, T2, T3 = dtk._build_quantities(*quantities)

        """

        nconv = self._name_converter
        quantities = set(quantities)
        # quantities are divided into 4 categories:
        #   humidity ratios,
        #   those whose magnitude require a bit of cleaning,
        #   those depending upon other quantities to be computed,
        #   and those that can be taken 'as is'.
        hum_ratios = quantities.intersection({'ws', 'wr'})
        to_clean = quantities.intersection({'f', 'flowrt_r'})
        dependent = quantities.intersection(
            {'Qcond', 'Qev', 'Pcomp', 'Pel', 'Qloss_ev', 't'})
        enthalpies = quantities.intersection({f'h{i+1}' for i in range(9)})
        as_is = quantities - hum_ratios - to_clean - dependent - enthalpies

        raw_data = self.raw_data
        for key, value in kwargs.items():
            raw_data = raw_data[raw_data[key] == value]

        if enthalpies or dependent - {'Pel', 't'}:
            ref_dir = self.get('refdir', **kwargs)
            # majority of 0 = heating, majority of 1 = cooling
            heating = np.count_nonzero(ref_dir) < len(ref_dir) / 2

        for w in hum_ratios:
            T = self.get('T' + w.strip('w'), **kwargs).to('K').magnitude
            RH = self.get('RH' + w.strip('w'), **kwargs).to('ratio').magnitude
            self.quantities[w] = self.Q_(
                psychro('W', 'P', 101325, 'T', T, 'RH', RH),
                label='$\omega_{' + w.strip('w') + '}$',
                prop='absolute humidity',
                units='ratio'
            ).to('g/kg')

        if to_clean:
            f = raw_data[nconv.loc['f', 'col_names']].values
            f[f == 'UnderRange'] = 0
            f = f.astype(float) / 2 # actual compressor frequency
            if 'f' in to_clean:
                self.quantities['f'] = self.Q_(
                    f,
                    label=nconv.loc['f', 'labels'],
                    prop=nconv.loc['f', 'properties'],
                    units=nconv.loc['f', 'units']
                )
            if 'flowrt_r' in to_clean:
                flowrt_r = raw_data[nconv.loc['flowrt_r', 'col_names']].values
                flowrt_r[f == 0] = 0
                self.quantities['flowrt_r'] = self.Q_(
                    flowrt_r,
                    label=nconv.loc['flowrt_r', 'labels'],
                    prop=nconv.loc['flowrt_r', 'properties'],
                    units=nconv.loc['flowrt_r', 'units']
                )

        for quantity in dependent - {'Pel', 't'}:
            if heating:
                ref_states = {
                    'Qcond': 'pout T4 pout T6',
                    'Qev': 'pout T6 pin T9',
                    'Pcomp': 'pin T1 pout T2'}[quantity]
            else:
                ref_states = {
                    'Qcond': 'pout T9 pout T7',
                    'Qev': 'pout T7 pin T4',
                    'Pcomp': 'pin T1 pout T2',
                    'Qloss_ev': 'pin T4 pin T1'}[quantity]
            heat_params = self.get('flowrt_r ' + ref_states, **kwargs)
            pow_kW = self._heat(quantity, *heat_params).to('kW')
            self.quantities[quantity] = pow_kW

        if 'Pel' in dependent:
            Pel = np.add(*self.get('Pa Pb', **kwargs))
            self.quantities['Pel'] = self.Q_(Pel.magnitude,
                                             label='$P_{el}$',
                                             prop='electrical power',
                                             units=Pel.units).to('kW')

        if 't' in dependent:
            timestep = self.get_timestep()
            samples_number = len(raw_data.index)
            time_in_seconds = np.arange(samples_number) * timestep.seconds
            self.quantities['t'] = self.Q_(time_in_seconds, 'seconds',
                                           label='$t$', prop='time')

        for quantity in as_is:
            magnitude = raw_data[nconv.loc[quantity, 'col_names']].values
            self.quantities[quantity] = self.Q_(magnitude,
                label=nconv.loc[quantity, 'labels'],
                prop=nconv.loc[quantity, 'properties'],
                units=nconv.loc[quantity, 'units'])

        for enthalpy in enthalpies:
            state = int(enthalpy.strip('h'))
            if (heating and state in (7, 8, 9, 1) or
                not heating and state in (6, 5, 4, 3, 1)):
                pstate = 'in'
            elif (heating and state in range(2, 7) or
                  not heating and state in (2, 9, 8, 7)):
                pstate = 'out'
            else:
                raise ValueError('The enthalpy state must be between 1 and 9.')
            p, T = self.get(f'p{pstate} T{state}', **kwargs)
            h = properties('H', 'P', p.to('Pa').magnitude,
                           'T', T.to('K').magnitude, 'R410a')
            self.quantities[enthalpy] = self.Q_(h,
                                                label=f'$h_{state}$',
                                                prop='enthalpy',
                                                units='J/kg').to('kJ/kg')

    def get(self, variables, update=False, **kwargs):
        """
        Return specific quantities from a DataTaker as Quantity objects.

        All the specified quantities that are not yet in the DataTaker's
        quantities are added, then all the specified quantities are
        returned in the form of Quantity objects.

        Parameters
        ----------
        quantities : str with a combination of the following items,
                     separated by spaces
            {T1 T2 T3 T4 T5 T6 T7 T8 T9 Ts RHs ws Tr RHr wr Tin
             Tout Tamb Tdtk f RHout Tout_db refdir flowrt_r pin
             pout Pa Pb Pfan_out Pfan_in Ptot Qcond Qev Pcomp}
        update : boolean, default False
            If set to True, quantities already present in the
            `quantities` attribute will be overwritten.
        **kwargs
            Keyword arguments allow to return quantities corresponding
            only to certain group in raw_data.

        Returns
        -------
        xpint Quantity or iterable of xpint Quantity objects

        Examples
        --------
        >>> dtk = vpa.DataTaker()
        >>> T4, pout = dtk.get('T4 pout')

        >>> properties = 'T1 T2 T3 T4 T5 T6 T7'
        >>> T1, T2, T3, T4, T5, T6, T7 = dtk.get(properties)

        Get the properties only for the second specified file
        (if at least two were specified):

        >>> T4, pout = dtk.get('T4 pout', index_file=1)

        Get the properties for a specific test period:

        >>> period = '26/09 08:36 - 14:31'
        >>> T4, pout = dtk.get('T4 pout', test_period=period)

        """

        spec_units = {}
        quantities = variables.split()
        for i, variable in enumerate(variables.split()):
            if '/' in variable:
                quantity, unit = variable.split('/', 1)
                quantities[i] = quantity
                spec_units[quantity] = unit
        if update or self._groups != kwargs:
            self.quantities = {}
            self._build_quantities(*set(quantities), **kwargs)
        else:
            # Only build quantities not already in the DataTaker's quantities
            self._build_quantities(*(set(quantities) - set(self.quantities)),
                                   **kwargs)
        self._groups = kwargs

        def update_units(quantity):
            return self.quantities[quantity].to(spec_units.get(quantity))

        # Return a Quantity if there is only one element in quantities
        if len(quantities) > 1:
            return (update_units(quantity) for quantity in quantities)
        else:
            return update_units(quantities[0])

        return result

    @ureg.wraps(None, (None, ureg.second))
    def set_steady_state_limits(self, limits):
        """
        Set the 'steady_state_time' column in raw_data according to the
        provided limits.

        Parameter
        ---------
        limits : Quantity with dimension [time]
            The steady-state time levels used to group measurements.
            To include measurements lower than the smallest value,
            add -numpy.inf as first element.
            To include measurements higher than the largest value,
            add numpy.inf as last element.

        Example
        -------
        >>> dtk = vaplac.DataTaker()
        >>> limits = np.array([5, 10, 40, np.inf]) * dtk.ureg('min')
        >>> dtk.set_steady_state_limits(limits)
        >>> dtk.plot('Qcond Pel', 'Tr Tout', groupby='steady_state_time')

        """

        timestep = self.get_timestep().seconds
        steady_state_time = self.steady_state_steps_number() * timestep
        sst_bins = np.empty_like(steady_state_time, dtype=object)
        include_lb = limits[0] == -np.inf
        include_ub = limits[-1] == np.inf
        if include_lb:
            limits = limits[1:]
        if include_ub:
            limits = limits[:-1]
        dimlimits = limits * self.ureg('s')
        if limits[1] >= 60:
            dimlimits.ito('minutes')

        def lbound(bound):
            return f'$\\tau_{{ss}} <$ {bound:.0f~P}'

        def ubound(bound):
            return f'$\\tau_{{ss}} \\geq$ {bound:.0f~P}'

        def between(lbound, ubound):
            return f'{lbound:.0f~P} $\\leq \\tau_{{ss}} <$ {ubound:.0f~P}'

        for i, binidx in enumerate(np.digitize(steady_state_time, limits) - 1):
            if binidx == -1:
                sst_bins[i] = lbound(dimlimits[0]) if include_lb else None
            elif binidx == len(limits) - 1:
                sst_bins[i] = ubound(dimlimits[-1]) if include_ub else None
            else:
                sst_bins[i] = between(dimlimits[binidx], dimlimits[binidx+1])
        self.raw_data['steady_state_time'] = sst_bins


    def plot(self, dependents='all', independents='t/minutes', groupby=None,
             **kwargs):
        """
        Plot DataTaker's quantities against time.

        If no quantities are given, all the Quantity objects in the
        DataTaker's attribute `quantities` are plotted. Each quantity
        having an identical dimensionality is plotted in the same axis.

        Parameters
        ----------
        dependents : {'all', 'allsplit', 'allmerge'} or str, default 'all'
            All the dependent quantities to be plotted, separated by a
            space. Quantites to be plotted together must be grouped
            inside (), [] or {}. A specific unit can also be given to a
            quantity or a group, using the format quantity/unit.
        independents : str, default 't/minutes'
            All the independent quantities to be plotted, separated by a
            space. Independent quantities cannot be grouped together, as
            opposed to dependent ones.
        groupby : str, default None
            The groups that will be highlighted in the plots.
            If not None, dependent quantities cannot be grouped.
        **kwargs : see function vaplac.plot.

        Examples
        --------
        Unit specification rely on pint and is therefore really
        felxible. For exemple, use teraelectronvolt per nanosecond
        for power :

        >>> dtk = vpa.DataTaker()
        >>> dtk.plot('(T1 T2) (Qev Qcond)/TeV/ns', 'f/rpm, t/hour')

        Group by test period:

        >>> dtk.plot('Qev Qcond', groupby='test_period')

        """

        # Store in a list the arguments to pass
        # to the datataker.plot method
        args = [] # parameters to pass to plot function
        dependents = 'allmerge' if dependents == 'all' else dependents

        # Define an iterator and an appender to add the right quantities
        # to the args list
        if dependents == 'allsplit':
            iterator = self.dependents.keys()
            appender = lambda arg: self.get(arg)
        elif dependents == 'allmerge':
            def gen():
                # Group dependents by property
                key = lambda q: self.dependents[q].prop
                for _, prop in groupby(sorted(self.dependents, key=key), key):
                    # Yield a list in any case, the appender will take
                    # care of the cases with only one element
                    yield [self.dependents[q] for q in prop]
            iterator = gen()
            appender = lambda arg: arg[0] if len(arg) == 1 else arg
        elif any(delim in dependents for delim in ('(', '[', '{')):
            # Split but keep grouped quantities together
            iterator = [arg.strip('()')
                        for arg in re.findall(r'\([^\)]*\)|\S+', dependents)]
            # Distribute any unit specified over a group
            if ')/' in dependents:
                for i, arg in enumerate(iterator):
                    if arg.startswith('/'):
                        unit = arg[1:]
                        group = iterator[i-1]
                        iterator[i-1] = f'/{unit} '.join(group.split(' '))
                        iterator[i-1] += f'/{unit}'
                        del iterator[i]
            def appender(arg):
                if ' ' in arg:
                    return list(self.get(arg))
                else:
                    return self.get(arg)
        else:
            iterator = dependents.split()
            if groupby:

                def quantity_from_group(arg, groupby, key):
                    quantity = self.get(arg, **{groupby: key})
                    quantity.group = key
                    return quantity

                appender = lambda arg: [quantity_from_group(arg, groupby, key)
                                        for key in self.raw_data.groupby(
                                            groupby).indices]
            else:
                appender = lambda arg: self.get(arg)

        for arg in iterator:
            args.append(appender(arg))

        if groupby:
            commons = [[quantity_from_group(common, groupby, key)
                        for key in self.raw_data.groupby(groupby).indices]
                        for common in independents.split()]
        else:
            commons = [self.get(common) for common in independents.split()]

        plot(*args, commons=commons, **kwargs)

    @ureg.wraps(None, (None, None, ureg.kilogram/ureg.second,
                       ureg.pascal, ureg.kelvin, ureg.pascal, ureg.kelvin))
    def _heat(self, power, flow=None,
              pin=None, Tin=None, pout=None, Tout=None):
        """
        Compute heat transfer rate from thermodynamic quantities.

        All provided quantities must be (x)pint Quantity objects, with a
        magnitude of the same length.

        Parameters
        ----------
        power : {'Qcond', 'Qev', 'Pcomp'}
            Property to be evaluated.
        flow : Quantity
            The mass flow rate of the fluid exchanging heat or work.
        pin : Quantity
            Inlet fluid pressure.
        Tin : Quantity
            Inlet fluid temperature.
        pout : Quantity
            Outlet fluid pressure.
        Tout : Quantity
            Outlet fluid temperature.

        Returns
        -------
        Quantity
            Mass flow rate multiplied by the enthalpy difference,
            i.e. the heat transfer rate in watts.

        """

        # Get the enthalpies using CoolProp, in J/kg
        hin = properties('H', 'P', pin, 'T', Tin, 'R410a')
        hout = properties('H', 'P', pout, 'T', Tout, 'R410a')

        # Check the phase, because points in and out may be
        # on the wrong side of the saturation curve
        phase_in = np.array([phase('P', p, 'T', T, 'R410a')
                             for p, T in zip(pin, Tin)])
        phase_out = np.array([phase('P', p, 'T', T, 'R410a')
                              for p, T in zip(pout, Tout)])

        # Assign the expected phases based on the specified property
        exp_phase_in, exp_phase_out = {'Qcond': ('gas', 'liq'),
                                       'Qev': ('liq', 'gas'),
                                       'Pcomp': ('gas', 'gas'),
                                       'Qloss_ev': ('gas', 'gas')}[power]
        # Get quality based on expected phase
        quality = {'liq':0, 'gas':1, None:None}

        # Replace by saturated state enthalpy if not in the right phase
        if not exp_phase_in in phase_in:
            hin[phase_in != exp_phase_in] = properties(
                'H', 'P', pin[phase_in != exp_phase_in],
                'Q', quality[exp_phase_in], 'R410a'
            )
        if not exp_phase_out in phase_out:
            hout[phase_out != exp_phase_out] = properties(
                'H', 'P', pout[phase_out != exp_phase_out],
                'Q', quality[exp_phase_out], 'R410a'
            )

        # Get the right attributes depending on the input property
        label={'Qcond': '$\dot{Q}_{cond}$',
               'Qev': '$\dot{Q}_{ev}$',
               'Pcomp': '$P_{comp}$',
               'Qloss_ev': '$\dot{Q}_{loss,ev}$'}[power]
        prop = 'mechanical power' if power == 'Pcomp' else 'heat transfer rate'

        # Return result in watts
        return self.Q_(flow * (hout - hin) * (-1 if power == 'Qcond' else 1),
                       label=label, units='W', prop=prop)

    @ureg.wraps(None, (None, ureg.hertzs))
    def steady_state_steps_number(self, sd_limit=Q_('2 Hz')):
        """
        Return the number of time steps in steady-state.

        Parameter
        ---------
        sd_limit : Quantity with dimension [1/time], default 2 Hz.
            The standard deviation limit that the frequency should not exceed
            in order to stay in steady-state regime.

        Returns
        -------
        steps_number : ndarray
            An array with the steady-state time interval of each measurement.

        """

        frequency = self.get('f').to('Hz').magnitude
        mean = np.empty_like(frequency)
        var = np.empty_like(frequency)
        steps_number = np.empty_like(frequency)
        mean[0] = frequency[0]
        var[0] = 0
        buffer = 1
        for i, f in enumerate(frequency[1:]):
            mean[i] = (buffer*mean[i-1] + f) / (buffer + 1)
            var[i] = ((buffer*(var[i-1] + mean[i-1]**2) + f**2) / (buffer + 1)
                      - mean[i]**2)
            buffer = buffer + 1
            if sqrt(var[i]) > sd_limit:  # standard deviation higher than 2 Hz
                mean[i] = f
                var[i] = 0
                steps_number[i-buffer:i] = buffer
                buffer = 0
        steps_number[-buffer:] = buffer
        return steps_number

    def validate(self, show_data=False):
        """
        Perform data checks implemented in vaplac.sauroneye.

        If no abnormalities are detected, the message 'No warnings' is
        displayed. Otherwise, the corresponding warnings will be given.

        Parameters
        ----------
        show_data : boolean, default False
            If set to True, the quantities involved in the checks
            resulting in a warning are plotted.

        Example
        -------
        >>> dtk = vpa.DataTaker()
        >>> dtk.validate(show_data=True)

        """
        schema = {check: {'check_with': getattr(sauroneye, check)}
                  for check in dir(sauroneye) if check.endswith('check')}
        v = Validator(schema)
        if v.validate({check: self for check in schema}):
            print('No warnings')
        else:
            n_warn = len(v.errors)
            if n_warn > 1:
                print(f'There are {n_warn} warnings:')
                for i, warning in enumerate(v.errors.values()):
                    print(' ', i+1, warning[0])
            else:
                warn = list(v.errors.values())[0][0]
                print('Warning:', warn[0].lower() + warn[1:] if warn else warn)

            if show_data:
                checkargs = sauroneye._checkargs
                args = ' '.join(checkargs[check] for check in v.errors)
                self.plot(args)
