from brian2 import SpikeMonitor, StateMonitor, SpikeGeneratorGroup
from brian2 import run, ms, mV
from brian2 import device
from brian2 import TimedArray

from core.utils.misc import decimal2minifloat

from core.equations.neurons.fp8LIF import fp8LIF
from core.equations.synapses.fp8CUBA import fp8CUBA
from core.equations.neurons.LIF import LIF
from core.equations.neurons.LIFIP import LIFIP
from core.equations.synapses.CUBA import CUBA
from core.equations.synapses.STDP import STDP
from core.builder.groups_builder import create_synapses, create_neurons
from core.utils.testbench import create_item, create_sequence, create_testbench

import numpy as np
import matplotlib.pyplot as plt
import json
from random import uniform, sample

import neo
import quantities as q
from elephant import statistics, kernels
from elephant.statistics import isi, cv
from elephant.conversion import BinnedSpikeTrain

from viziphant.statistics import plot_instantaneous_rates_colormesh
from brian2tools import brian_plot, plot_state

from sklearn.linear_model import LogisticRegression


def liquid_state_machine(defaultclock, trial_no, path, quiet):
    output_mod = 'stdp'
    precision = 'fp64'
    # TODO
    #import random
    #random.seed(25)
    #from brian2 import seed
    #np.random.seed(25)
    #seed(25)

    if precision == 'fp8':
        liquid_neu = fp8LIF
        liquid_syn = fp8CUBA
    elif precision == 'fp64':
        liquid_neu = LIF
        liquid_syn = CUBA

    item_rate = 128
    repetitions = 140
    inter_spk_interval = np.ceil(1/item_rate*1000).astype(int)
    inter_seq_interval = 200
    item_spikes = 1
    A = create_item([0], inter_spk_interval, item_spikes)
    B = create_item([1], inter_spk_interval, item_spikes)
    C = create_item([2], inter_spk_interval, item_spikes)
    D = create_item([3], inter_spk_interval, item_spikes)
    E = create_item([4], inter_spk_interval, item_spikes)
    F = create_item([5], inter_spk_interval, item_spikes)
    G = create_item([6], inter_spk_interval, item_spikes)
    H = create_item([7], inter_spk_interval, item_spikes)

    seq1 = [A, B, C, D, E, F, G, H]
    seq2 = [H, G, F, E, D, C, B, A]
    seq1 = create_sequence(seq1, inter_spk_interval)
    seq2 = create_sequence(seq2, inter_spk_interval)

    channels_per_item = 1
    num_items = 8
    num_seq = 2
    print(f'Simulation with {num_seq} sequences, each having {num_items} '
          f'items represented by {channels_per_item} input channels')

    input_indices, input_times, events = create_testbench([seq1, seq2],
                                                          [.5, .5],
                                                          inter_seq_interval,
                                                          repetitions)
    input_indices = np.array(input_indices)
    input_times = np.array(input_times) * ms
    sequence_duration = max(seq1['times'])
    num_channels = int(max(input_indices) + 1)
    sim_dur = repetitions*(sequence_duration+inter_seq_interval)*ms
    test_size = 50
    test_dur = test_size*(sequence_duration+inter_seq_interval)*ms
    input_spikes = SpikeGeneratorGroup(num_channels,
                                       input_indices,
                                       input_times)

    # TODO sizes from 128, 256, 512, 1024, 2048, 4096. Original was 4084
    Nt = 128
    Ne, Ni = np.rint(Nt*.85).astype(int), np.rint(Nt*.15).astype(int)
    # In case rounding makes a difference
    Nt = Ne + Ni

    e_neu_model = liquid_neu()
    # TODO noise
    #rand_samples = [uniform(0, 1)
    #                for _ in range(int((sim_dur/defaultclock.dt)*Nt))]
    #rand_samples = np.reshape(rand_samples, (int(sim_dur/defaultclock.dt), Nt))
    #noise = TimedArray(rand_samples*mV, dt=defaultclock.dt)
    #e_neu_model.modify_model('model', 'alpha*Vm + noise(t, i)', old_expr='alpha*Vm')
    e_neu_model.model += 'x : integer (constant)\ny : integer (constant)\nz : integer (constant)\n'
    if precision == 'fp64':
        e_neu_model.modify_model('model',
                               'gtot = gtot0 + gtot1 + gtot2 + gtot3',
                               old_expr='gtot = gtot0')
        e_neu_model.model += 'gtot1 : volt\ngtot2 : volt\ngtot3 : volt\n'
    cells = create_neurons(Nt, e_neu_model)

    # Random placement in a grid
    net_grid = np.reshape(sample(range(Nt), k=Nt), (4, 2, 16))
    for neu_id in np.nditer(net_grid):
        positions = np.where(net_grid==int(neu_id))
        cells[int(neu_id)].x = str(positions[0][0])
        cells[int(neu_id)].y = str(positions[1][0])
        cells[int(neu_id)].z = str(positions[2][0])

    exc_cells = cells[:Ne]
    inh_cells = cells[Ne:]

    e_syn_model = liquid_syn()
    e_syn_model.modify_model('connection', .12, key='p')
    if precision == 'fp8':
        e_syn_model.modify_model('parameters',
                                 decimal2minifloat(96),
                                 key='weight')
    if precision == 'fp64':
        e_syn_model.modify_model('parameters', 80*mV, key='weight')
        e_syn_model.modify_model('model', 'gtot1_post', old_expr='gtot0_post')
    thl_conns = create_synapses(input_spikes, cells, e_syn_model)

    e_syn_model = liquid_syn()
    e_syn_model.modify_model('connection', .1, key='p')
    e_syn_model.modify_model('parameters', '20*rand()*ms', key='delay')
    if precision == 'fp8':
        e_syn_model.modify_model('parameters',
                                 decimal2minifloat(24),
                                 key='weight')
    elif precision == 'fp64':
        e_syn_model.modify_model('model', 'gtot2_post', old_expr='gtot0_post')
        e_syn_model.modify_model('parameters', 20*mV, key='weight')
    intra_exc = create_synapses(exc_cells, cells, e_syn_model)

    i_syn_model = liquid_syn()
    i_syn_model.modify_model('connection', .1, key='p')
    if precision == 'fp8':
        i_syn_model.modify_model('namespace',
                                 decimal2minifloat(-1),
                                 key='w_factor')
        i_syn_model.modify_model('parameters',
                                 decimal2minifloat(120),
                                 key='weight')
    if precision == 'fp64':
        i_syn_model.modify_model('namespace', -1, key='w_factor')
        i_syn_model.modify_model('parameters', 100*mV, key='weight')
        i_syn_model.modify_model('model', 'gtot3_post', old_expr='gtot0_post')
    intra_inh = create_synapses(inh_cells, cells, i_syn_model)

    e_neu_model = LIFIP()
    e_neu_model.modify_model('namespace', 90000*ms, key='tau_thr')
    e_neu_model.modify_model('namespace', 0.1*mV, key='thr_inc')
    e_neu_model.modify_model('parameters', 20*mV, key='Vthr')
    e_neu_model.modify_model('refractory', '20*ms')
    e_neu_model.modify_model('model', 'gtot = gtot0 + gtot1',
                             old_expr='gtot = gtot0')
    e_neu_model.model += 'gtot1 : volt\n'
    e_neu_model.model += 'inc_w : volt\n'
    readout = create_neurons(num_seq, e_neu_model, name='readout')

    labels_indices = []
    labels_times = []
    for ev in events:
        labels_indices.append(ev[0])
        labels_times.append(ev[2])
    labels = SpikeGeneratorGroup(num_seq, labels_indices, labels_times)

    if output_mod == 'stdp':
        e_syn_model = STDP()
        e_syn_model.modify_model('on_pre', 'g_syn += w_plast',
                                 old_expr='g += w_plast')
        e_syn_model.modify_model('parameters', 0.02*mV, key='w_plast')
        e_syn_model.modify_model('namespace', 10*mV, key='eta')
        # TODO do i need this? I DONT think so
        #e_syn_model.modify_model('parameters',
        #                         f'{sequence_duration}*rand()*ms',
        #                         key='delay')
        e_syn_model.model += 'inc_w_post = w_plast : volt (summed)\n'
        norm_factor = 1
        e_syn_model.on_post += 'w_plast = int(norm_factor==1)*(w_plast/inc_w_post*mV) + int(norm_factor==0)*w_plast'
    elif output_mod == 'delay':
        e_syn_model = CUBA()

        e_syn_model.modify_model('on_pre', 'g_syn += weight',
                                 old_expr='g += weight')
        e_syn_model.model += 'delta_t : second\ndelay_proxy : second\n'
        e_syn_model.on_post += f'delta_t = clip(t - lastspike_pre, 0*ms, {sequence_duration + 1}*ms)\n'
        e_syn_model.on_post += 'delay_proxy = delay_proxy - 1*(delay_proxy - delta_t)\n'
        e_syn_model.parameters = {**e_syn_model.parameters,
                                  'delay_proxy': '0*ms'}
        e_syn_model.modify_model('parameters', 0.02*mV, key='weight')
    e_syn_model.modify_model('model', 'dg_syn/dt = alpha_syn*g_syn',
                             old_expr='dg/dt = alpha_syn*g')
    e_syn_model.modify_model('model', 'g_syn*w_factor',
                             old_expr='g*w_factor')
    e_syn_model.modify_model('model', 'tau_syn_syn', old_expr='tau_syn')
    e_syn_model.parameters = {**e_syn_model.parameters,
                              **{'tau_syn_syn': '5*ms'}}
    e_syn_model.modify_model('model', 'alpha_syn_syn',
                             old_expr='alpha_syn')
    del e_syn_model.parameters['tau_syn']
    e_syn_model.parameters = {**e_syn_model.parameters,
                              **{'alpha_syn_syn': 'tau_syn_syn/(dt + tau_syn_syn)'}}
    del e_syn_model.parameters['alpha_syn']
    exc_readout = create_synapses(exc_cells, readout, e_syn_model,
                                  name='exc_readout')

    e_syn_model = CUBA()
    e_syn_model.modify_model('model', 'dg_syn/dt = alpha_syn*g_syn',
                             old_expr='dg/dt = alpha_syn*g')
    e_syn_model.modify_model('model', 'gtot1_post = g_syn*w_factor',
                             old_expr='gtot0_post = g*w_factor')
    e_syn_model.modify_model('on_pre', 'g_syn += weight',
                             old_expr='g += weight')
    e_syn_model.modify_model('connection', 'i', key='j')
    e_syn_model.modify_model('parameters', 200*mV, key='weight')
    label_readout = create_synapses(labels, readout, e_syn_model,
                                    name='label_readout')

    selected_exc_cells = np.random.choice(Ne, 4, replace=False)
    selected_inh_cells = np.random.choice(Ni, 4, replace=False)

    Metadata = {'selected_exc_cells': selected_exc_cells.tolist(),
                'selected_inh_cells': selected_inh_cells.tolist(),
                'dt': str(defaultclock.dt),
                'trial_no': trial_no,
                'duration': str(sim_dur),
                'inh_weight': str(i_syn_model.parameters['weight'])}
    with open(path+'metadata.json', 'w') as f:
        json.dump(Metadata, f)

    spkmon_e = SpikeMonitor(exc_cells)
    spkmon_i = SpikeMonitor(inh_cells)
    spkmon_ro = SpikeMonitor(readout)
    sttmon_e = StateMonitor(exc_cells, variables='Vm',
                            record=selected_exc_cells)
    sttmon_i = StateMonitor(inh_cells, variables='Vm',
                            record=selected_inh_cells)
    sttmon_ro = StateMonitor(readout, variables=['Vm', 'Vthr'],
                             record=[0, 1])

    kernel = kernels.GaussianKernel(sigma=30*q.ms)
    run(sim_dur-test_dur)

    if output_mod == 'delay':
        exc_readout.delay = 'delay_proxy'
        exc_readout.weight['delta_t==129*ms'] = 0*mV
    # TODO small weights e.g. 10 get stuck, high explodes e.g. 56. 1 kindda
    # works. 60 for small net
    # e_syn_model.modify_model('parameters', 60, key='weight')
    label_readout.namespace['w_factor'] = 0
    exc_readout.namespace['eta'] = 0*mV
    norm_factor = 0
    exc_readout.w_plast = '145*(w_plast/inc_w_post*mV)'
    run(test_dur)
    device.build()

    # Process data for measuring accuracy
    neo_spks = []
    for spk_train in spkmon_e.spike_trains().values():
        neo_spks.append(neo.SpikeTrain(spk_train/ms*q.ms,
                                       t_stop=sim_dur/ms*q.ms))
    data = BinnedSpikeTrain(neo_spks, bin_size=8*q.ms)
    samples = []
    for lt in labels_times:
        # Not casting like below results in error!
        ti = np.around(lt/ms - sequence_duration).astype(int)*q.ms
        tf = np.around(lt/ms).astype(int)*q.ms
        temp_data = data.time_slice(ti, tf).to_array()
        samples.append(temp_data.flatten())

    # TODO not working with more gap
    #samples_size = len(labels_times)
    #train_size = samples_size-test_size
    #lr = LogisticRegression()
    #lr.fit(samples[:train_size], labels_indices[:train_size])
    #score = lr.score(samples[train_size:], labels_indices[train_size:])
    #print('\n###################')
    #print(f'Accuracy is {score}\n')

    temp_trains = spkmon_e.spike_trains()
    spk_trains = [neo.SpikeTrain(temp_trains[x]/ms, t_stop=sim_dur/ms, units='ms')
                  for x in temp_trains]
    pop_rates = statistics.instantaneous_rate(spk_trains,
                                              sampling_period=1*q.ms,
                                              kernel=kernel)
    pop_avg_rates = np.mean(pop_rates, axis=1)

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
        fig,  (ax0, ax1, ax2, ax3) = plt.subplots(4, 1, sharex=True)
        plot_state(sttmon_ro.t, sttmon_ro.Vm[0], var_unit=mV, axes=ax0)
        plot_state(sttmon_ro.t, sttmon_ro.Vm[1], var_unit=mV, axes=ax0)
        plot_state(sttmon_ro.t, sttmon_ro.Vthr[0], var_unit=mV, axes=ax0)
        plot_state(sttmon_ro.t, sttmon_ro.Vthr[1], var_unit=mV, axes=ax0)
        brian_plot(spkmon_ro, axes=ax3)
        ax0.vlines((sim_dur-test_dur)/ms, 0, 1,
                   transform=ax0.get_xaxis_transform(), colors='r')
        brian_plot(spkmon_e, axes=ax1)
        ax2.plot(input_times/ms, input_indices, '.')

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

        # TODO better organize somewhere else
        output_spikes = []
        for spk in spkmon_ro.spike_trains().values():
            output_spikes.append(
                neo.SpikeTrain(
                    spk,
                    units='ms',
                    t_stop=np.around(sim_dur/defaultclock.dt).astype(int)))
        import feather
        import pandas as pd
        output_spikes = pd.DataFrame(
            {'time': np.array(spkmon_ro.t/defaultclock.dt),
             'id': np.array(spkmon_ro.i)})
        feather.write_dataframe(output_spikes, 'output_spikes.feather')
        events = np.array([[ev[0], ev[1]/defaultclock.dt, ev[2]/defaultclock.dt] for ev in events])
        events = pd.DataFrame(events, columns=['label', 't_start', 't_stop'])
        feather.write_dataframe(events, 'input_spikes.feather')
        plt.show()
