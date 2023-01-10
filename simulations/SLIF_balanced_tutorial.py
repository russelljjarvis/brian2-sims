from brian2 import PoissonGroup, SpikeMonitor, StateMonitor
from brian2 import defaultclock, prefs, Network, collect, device, get_device,\
        set_device, run
from brian2 import second, Hz, ms, ohm, mA, mV, Network

from core.utils.misc import minifloat2decimal, decimal2minifloat
from core.utils.prepare_models import generate_connection_indices

from core.parameters.orca_params import ConnectionDescriptor, PopulationDescriptor

from core.equations.neurons.LIF import LIF
from core.equations.synapses.CUBA import CUBA
from core.equations.neurons.fp8LIF import fp8LIF
from core.equations.synapses.fp8CUBA import fp8CUBA
from core.equations.neurons.int4LIF import int4LIF
from core.equations.synapses.int4CUBA import int4CUBA
from core.equations.neurons.int8LIF import int8LIF
from core.equations.synapses.int8CUBA import int8CUBA
from core.builder.groups_builder import create_synapses, create_neurons

import sys
import os
from datetime import datetime
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import pickle
import json
import argparse

import neo
import quantities as q
from elephant import statistics, kernels
from elephant.statistics import isi, cv

from viziphant.statistics import plot_instantaneous_rates_colormesh
from brian2tools import brian_plot


# Code using orca column and standard parameters was obselete so it was removed.
# It can still be found in predictive learning and extrapolation, bbut it should
# be removed as well. This is because I am not creating a single motif that is to
# be repeated somewhere else with minor modifications anymore. I am rather just
# creating one network for each case. Previous examples can be found in previous 
# repository from gitlab
def balanced_network(args):
    defaultclock.dt = args.timestep * ms

    """ ==================== Input ==================== """
    poisson_spikes = PoissonGroup(285, rates=6*Hz)

    """ ==================== Models ==================== """
    Ne, Ni = 3471, 613

    neurons, exc_neurons, inh_neurons = [], [], []
    models = zip([LIF, int4LIF, int8LIF, fp8LIF],
                 ['fp64_neu', 'int4_neu', 'int8_neu', 'fp8_neu'])
    for model, name in models:
        aux_model = model()
        aux_model.model += 'gtot1 : volt\ngtot2 : volt\n'
        neurons.append(create_neurons(Ne+Ni,
                                      aux_model,
                                      name=name))
        exc_neurons.append(neurons[-1][:Ne])
        inh_neurons.append(neurons[-1][Ne:])

    thalamus_connections = []
    models = zip([CUBA, int4CUBA, int8CUBA, fp8CUBA],
                 neurons,
                 ['fp64_thal', 'int4_thal', 'int8_thal', 'fp8_thal'])
    for model, neu, name in models:
        aux_model = model()
        sources, targets = generate_connection_indices(poisson_spikes.N,
                                                       neu.N,
                                                       0.25)
        aux_model.modify_model('connection', sources, key='i')
        aux_model.modify_model('connection', targets, key='j')
        thalamus_connections.append(create_synapses(poisson_spikes,
                                                    neu,
                                                    aux_model,
                                                    name=name))

    intra_exc, intra_inh = [], []
    models = zip([CUBA, int4CUBA, int8CUBA, fp8CUBA],
                 exc_neurons,
                 inh_neurons,
                 neurons,
                 ['fp64_syn', 'int4_syn', 'int8_syn', 'fp8_syn'])
    for model, e_neu, i_neu, neu, name in models:
        aux_model = model()
        sources, targets = generate_connection_indices(e_neu.N, neu.N, .1)
        aux_model.modify_model('connection', sources, key='i')
        aux_model.modify_model('connection', targets, key='j')
        if name=='fp64_syn':
            aux_model.modify_model('model',
                                   'gtot1_post',
                                   old_expr='gtot0_post')
        intra_exc.append(create_synapses(e_neu, neu, aux_model, name=name+'_e'))

        aux_model = model()
        sources, targets = generate_connection_indices(i_neu.N, neu.N, .1)
        aux_model.modify_model('connection', sources, key='i')
        aux_model.modify_model('connection', targets, key='j')
        if name=='fp64_syn':
            aux_model.modify_model('model',
                                   'gtot2_post',
                                   old_expr='gtot0_post')
        if name=='fp8_syn':
            aux_model.modify_model('namespace',
                                   decimal2minifloat(-1),
                                   key='w_factor')
        else:
            aux_model.modify_model('namespace', -1, key='w_factor')
        aux_model.modify_model('parameters',
                               args.w_in if name!='fp64_syn' else args.w_in*mV,
                               key='weight')
        intra_inh.append(create_synapses(i_neu, neu, aux_model, name=name+'_i'))

    """ ==================== Monitors ==================== """
    rng = np.random.default_rng(12345)
    selected_exc_cells = rng.choice(Ne, 4, replace=False)
    selected_inh_cells = rng.choice(Ni, 4, replace=False)

    spkmon_e = [SpikeMonitor(x) for x in exc_neurons]
    spkmon_i = [SpikeMonitor(x) for x in inh_neurons]
    sttmon_e = [StateMonitor(x, variables='Vm',
                             record=selected_exc_cells)
                    for x in exc_neurons]
    sttmon_i = [StateMonitor(x, variables='Vm',
                             record=selected_inh_cells)
                    for x in inh_neurons]

    """ ==================== running/processing ==================== """
    duration = 1000
    net = Network()
    net.add(neurons, exc_neurons, inh_neurons, thalamus_connections, intra_exc,
            intra_inh, poisson_spikes, spkmon_e, spkmon_i, sttmon_e, sttmon_i)
    net.run(duration*ms)

    temp_trains = spkmon_e.spike_trains()
    spk_trains = [neo.SpikeTrain(temp_trains[x]/ms, t_stop=duration, units='ms')
                  for x in temp_trains]
    kernel = kernels.GaussianKernel(sigma=30*q.ms)
    pop_rates = statistics.instantaneous_rate(spk_trains,
                                              sampling_period=1*q.ms,
                                              kernel=kernel)
    pop_avg_rates = np.mean(pop_rates, axis=1)

    """ ==================== saving results ==================== """
    Metadata = {'selected_exc_cells': selected_exc_cells.tolist(),
                'selected_inh_cells': selected_inh_cells.tolist(),
                'dt': str(defaultclock.dt),
                'trial_no': trial_no,
                'duration': str(duration*ms),
                'inh_weight': i_syn_model.parameters['weight']}
    with open(path+'metadata.json', 'w') as f:
        json.dump(Metadata, f)

    np.savez(f'{path}/exc_raster.npz',
             times=spkmon_e.t/ms,
             indices=spkmon_e.i)
    np.savez(f'{path}/inh_raster.npz',
             times=spkmon_i.t/ms,
             indices=spkmon_i.i)
    np.savez(f'{path}/rates.npz',
             times=np.array(pop_rates.times),
             rates=np.array(pop_avg_rates))

    if not quiet:
        fig, ax1 = plt.subplots()
        ax2 = ax1.twinx()
        ax2.plot(pop_rates.times, pop_avg_rates, color='red')
        brian_plot(spkmon_e, marker=',', color='black', axes=ax1)
        ax1.set_xlabel(f'time ({pop_rates.times.dimensionality.latex})')
        ax1.set_ylabel('neuron number')
        ax2.set_ylabel(f'rate ({pop_rates.dimensionality})')

        plot_instantaneous_rates_colormesh(pop_rates)
        plt.title('Neuron rates on last trial')

        isi_neu = [isi(spks) for spks in spk_trains]
        fig, ax3 = plt.subplots()
        flatten_isi = []
        for vals in isi_neu:
            flatten_isi = np.append(flatten_isi, vals)
        ax3.hist(flatten_isi, bins=np.linspace(-3, 100, 10))
        ax3.set_title('ISI distribution')
        ax3.set_xlabel('ISI')
        ax3.set_ylabel('count')

        plt.figure()
        cv_neu = [cv(x) for x in isi_neu]
        plt.hist(cv_neu)
        plt.title('Coefficient of variation')
        plt.ylabel('count')
        plt.xlabel('CV')

        plt.show()
