import time
import numpy as np

import sklearn.metrics

from .peeler_tools import *
from .peeler_tools import _dtype_spike
from . import signalpreprocessor

from .cltools import HAVE_PYOPENCL, OpenCL_Helper


from .peeler_engine_base import PeelerEngineGeneric

# import kernels
#~ from .signalpreprocessor import processor_kernel
from .peakdetector import opencl_kernel_geometrical_part2


try:
    import pyopencl
    mf = pyopencl.mem_flags
    HAVE_PYOPENCL = True
except ImportError:
    HAVE_PYOPENCL = False

import matplotlib.pyplot as plt


class PeelerEngineGeometricalCl(PeelerEngineGeneric):
    def change_params(self, adjacency_radius_um=100, **kargs): # high_adjacency_radius_um=50, 
        assert HAVE_PYOPENCL
        
        PeelerEngineGeneric.change_params(self, **kargs)
        
        assert self.use_sparse_template
        
        self.adjacency_radius_um = adjacency_radius_um
        
        self.shifts = np.arange(-self.maximum_jitter_shift, self.maximum_jitter_shift+1)
        


    def initialize(self, **kargs):
        assert not self.save_bad_label # TODO add this feature when this work
        
        assert self.internal_dtype == 'float32'
        
        if self.argmin_method == 'opencl':
            OpenCL_Helper.initialize_opencl(self, cl_platform_index=self.cl_platform_index, cl_device_index=self.cl_device_index)
        
        PeelerEngineGeneric.initialize(self, **kargs)
        
        
        # make neighbours for peak detector CL
        d = sklearn.metrics.pairwise.euclidean_distances(self.geometry)
        self.channel_distances = d.astype('float32')
        neighbour_mask = d<=self.catalogue['peak_detector_params']['adjacency_radius_um']
        nb_neighbour_per_channel = np.sum(neighbour_mask, axis=0)
        nb_max_neighbour = np.max(nb_neighbour_per_channel)
        self.nb_max_neighbour = nb_max_neighbour # include itself
        self.neighbours = np.zeros((self.nb_channel, nb_max_neighbour), dtype='int32')
        self.neighbours[:] = -1
        for c in range(self.nb_channel):
            neighb, = np.nonzero(neighbour_mask[c, :])
            self.neighbours[c, :neighb.size] = neighb
        
        #~ self.mask_peaks = np.zeros((self.fifo_size - 2 * self.n_span, self.nb_channel), dtype='uint8')  # bool
        
        # debug to check same ctx and queue as processor
        if self.signalpreprocessor is not None:
            assert self.ctx is self.signalpreprocessor.ctx
        
        # make kernels
        centers = self.catalogue['centers0']
        self.nb_cluster = centers.shape[0]
        self.peak_width = centers.shape[1]
        self.nb_channel = centers.shape[2]
        
        self.nb_shift = self.shifts.size
        
        wf_size = self.peak_width * self.nb_channel
        
        
        kernel_formated = kernel_peeler_cl % dict(
                    chunksize=self.chunksize,
                    n_span=self.n_span,
                    nb_channel=self.nb_channel,
                    nb_cluster=self.nb_cluster,
                    relative_threshold=self.relative_threshold,
                    peak_sign={'+':1, '-':-1}[self.peak_sign],
                    extra_size=self.extra_size,
                    fifo_size=self.fifo_size,
                    n_left=self.n_left,
                    n_right=self.n_right,
                    peak_width=self.peak_width,
                    maximum_jitter_shift=self.maximum_jitter_shift,
                    n_cluster=self.nb_cluster,
                    wf_size=wf_size,
                    subsample_ratio=self.catalogue['subsample_ratio'],
                    nb_neighbour=self.nb_max_neighbour, 
                    inter_sample_oversampling=int(self.inter_sample_oversampling),
                    nb_shift=self.nb_shift,
                    )
        #~ print(kernel_formated)
        prg = pyopencl.Program(self.ctx, kernel_formated)
        self.opencl_prg = prg.build(options='-cl-mad-enable')
        
        
        

        # create CL buffers
        self.fifo_residuals_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.fifo_residuals)
        
        self.channel_distances_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.channel_distances)
        
        self.sigs_chunk = np.zeros((self.chunksize, self.nb_channel), dtype='float32')
        self.sigs_chunk_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.sigs_chunk)

        self.neighbours_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.neighbours)
        


        self.waveform_distance = np.zeros((self.nb_cluster), dtype='float32')
        self.waveform_distance_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.waveform_distance)
        
        self.waveform_distance_shifts = np.zeros((self.shifts.size, ), dtype='float32')
        self.waveform_distance_shifts_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.waveform_distance_shifts)
        

        self.sparse_mask_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.sparse_mask.astype('u1'))
        self.high_sparse_mask_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.high_sparse_mask.astype('u1'))
        
        
        dtype_spike = [('peak_index', 'int32'), ('cluster_idx', 'int32'), ('jitter', 'float32')]
        
        self.next_spike = np.zeros(1, dtype=dtype_spike)
        self.next_spike_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.next_spike)

        self.catalogue_center0_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['centers0'].astype('float32'))
        self.catalogue_center1_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['centers1'].astype('float32'))
        self.catalogue_center2_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['centers2'].astype('float32'))
        self.catalogue_inter_center0_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['interp_centers0'].astype('float32'))
        
        
        self.extremum_channel_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['extremum_channel'].astype('int32'))
        self.wf1_norm2_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['wf1_norm2'].astype('float32'))
        self.wf2_norm2_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['wf2_norm2'].astype('float32'))
        self.wf1_dot_wf2_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.catalogue['wf1_dot_wf2'].astype('float32'))

        self.weight_per_template = np.zeros((self.nb_cluster, self.nb_channel), dtype='float32')
        centers = self.catalogue['centers0']
        for i, k in enumerate(self.catalogue['cluster_labels']):
            mask = self.sparse_mask[i, :]
            wf = centers[i, :, :][:, mask]
            self.weight_per_template[i, mask] = np.sum(wf**2, axis=0)
        self.weight_per_template_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.weight_per_template)
        
        
        # TODO
        nb_max_spike_in_chunk = self.nb_channel * self.fifo_size
        print('nb_max_spike_in_chunk', nb_max_spike_in_chunk)
        
        
        dtype_peak = [('peak_index', 'int32'), ('peak_chan', 'int32'), ('peak_value', 'float32')]
        
        self.next_peak = np.zeros(1, dtype=dtype_peak)
        self.next_peak_cl =  pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.next_peak)
        
        self.pending_peaks = np.zeros(nb_max_spike_in_chunk, dtype=dtype_peak)
        self.pending_peaks_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.pending_peaks)
        
        self.nb_pending_peaks = np.zeros(1, dtype='int32')
        self.nb_pending_peaks_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.nb_pending_peaks)

        self.good_spikes = np.zeros(nb_max_spike_in_chunk, dtype=dtype_spike)
        self.good_spikes_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.good_spikes)

        self.nb_good_spikes = np.zeros(1, dtype='int32')
        self.nb_good_spikes_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.nb_good_spikes)
        
        self.mask_already_tested = np.zeros((self.fifo_size, self.nb_channel), dtype='uint8') # bool
        self.mask_already_tested_cl = pyopencl.Buffer(self.ctx, mf.READ_WRITE| mf.COPY_HOST_PTR, hostbuf=self.mask_already_tested)
        
        

        # kernel calls
        self.kern_add_fifo_residuals = getattr(self.opencl_prg, 'add_fifo_residuals')
        fifo_roll_size = self.fifo_size - self.chunksize
        self.kern_add_fifo_residuals.set_args(self.fifo_residuals_cl,
                                                                self.sigs_chunk_cl,
                                                                np.int32(fifo_roll_size))
        
        self.kern_detect_local_peaks = getattr(self.opencl_prg, 'detect_local_peaks')
        self.kern_detect_local_peaks.set_args(self.fifo_residuals_cl,
                                                                self.neighbours_cl,
                                                                self.mask_already_tested_cl,
                                                                self.pending_peaks_cl,
                                                                self.nb_pending_peaks_cl)
        
        
        if self.alien_value_threshold is None:
            alien_value_threshold = np.float32(0)
        else:
            alien_value_threshold = np.float32(self.alien_value_threshold)
        #~ print('alien_value_threshold', alien_value_threshold)


        self.kern_select_next_peak = getattr(self.opencl_prg, 'select_next_peak')
        self.kern_select_next_peak.set_args(self.fifo_residuals_cl,
                                                            self.next_peak_cl,
                                                            self.pending_peaks_cl,
                                                            self.nb_pending_peaks_cl,
                                                            self.next_spike_cl,
                                                            np.float32(self.adjacency_radius_um),
                                                            alien_value_threshold,
                                                            )
    
        self.kern_get_template_distances = getattr(self.opencl_prg, 'get_template_distances')
        self.kern_get_template_distances.set_args(self.fifo_residuals_cl,
                                                        self.next_spike_cl,
                                                        self.next_peak_cl,
                                                        self.catalogue_center0_cl,
                                                        self.high_sparse_mask_cl,
                                                        self.waveform_distance_cl,
                                                        self.channel_distances_cl,
                                                        np.float32(self.adjacency_radius_um),
                                                        )
        
        self.kern_explore_shifts = getattr(self.opencl_prg, 'explore_shifts')
        self.kern_explore_shifts.set_args(self.fifo_residuals_cl,
                                                        self.next_spike_cl,
                                                        self.next_peak_cl,
                                                        self.catalogue_center0_cl,
                                                        self.sparse_mask_cl,
                                                        self.waveform_distance_cl,
                                                        self.waveform_distance_shifts_cl
                                                        )

        self.kern_finalize_next_spike = getattr(self.opencl_prg, 'finalize_next_spike')
        self.kern_finalize_next_spike.set_args(self.fifo_residuals_cl,
                                                        self.next_spike_cl,
                                                        self.next_peak_cl,
                                                        self.pending_peaks_cl,
                                                        self.nb_pending_peaks_cl,
                                                        self.good_spikes_cl,
                                                        self.nb_good_spikes_cl,
                                                        self.mask_already_tested_cl,
                                                        self.catalogue_center0_cl,
                                                        self.catalogue_center1_cl,
                                                        self.catalogue_center2_cl,
                                                        self.catalogue_inter_center0_cl,
                                                        self.sparse_mask_cl,
                                                        self.waveform_distance_shifts_cl,
                                                        self.extremum_channel_cl,
                                                        self.wf1_norm2_cl,
                                                        self.wf2_norm2_cl,
                                                        self.wf1_dot_wf2_cl,
                                                        self.weight_per_template_cl
                                                        )

        self.kern_reset_tested_zone = getattr(self.opencl_prg, 'reset_tested_zone')
        self.kern_reset_tested_zone.set_args(
                                                self.mask_already_tested_cl,
                                                self.good_spikes_cl,
                                                self.nb_good_spikes_cl,
                                                self.sparse_mask_cl,
                                                )

        
    def initialize_before_each_segment(self, **kargs):
        PeelerEngineGeneric.initialize_before_each_segment(self, **kargs)
        #~ self.mask_not_already_tested[:] = 1

    
    def apply_processor(self, pos, sigs_chunk):
        if self._plot_debug:
            print('apply_processor')
        assert sigs_chunk.shape[0]==self.chunksize
        
        if not sigs_chunk.flags['C_CONTIGUOUS'] or sigs_chunk.dtype!=self.internal_dtype:
            sigs_chunk = np.ascontiguousarray(sigs_chunk, dtype=self.internal_dtype)
        
        if self.already_processed:
            abs_head_index, preprocessed_chunk =  pos, sigs_chunk
            
            pyopencl.enqueue_copy(self.queue,  self.sigs_chunk_cl, sigs_chunk)
            n = self.fifo_residuals.shape[0]-self.chunksize
            global_size = (self.chunksize, self.nb_channel)
            local_size = (1, self.nb_channel)
            event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_add_fifo_residuals, global_size, local_size,)

        else:
            raise NotImplemenentedError
            # TODO call preprocessor kernel and add_fifo_residuals
            
            #~ abs_head_index, preprocessed_chunk = self.signalpreprocessor.process_data(pos, sigs_chunk)
        
        #~ if self._debug_cl:
            #~ pyopencl.enqueue_copy(self.queue,  self.fifo_residuals, self.fifo_residuals_cl)
            #~ fig, ax = plt.subplots()
            #~ ax.plot(self.fifo_residuals)
            #~ plt.show()

        return abs_head_index, preprocessed_chunk 

    def detect_local_peaks_before_peeling_loop(self):
        if self._plot_debug:
            print('detect_local_peaks_before_peeling_loop')

        #~ self.global_size =  (self.max_wg_size * n, )
        #~ self.local_size = (self.max_wg_size,)
        
        # reset mask_already_tested
        #~ print('yep', self.mask_already_tested.size, self.mask_already_tested.shape)
        #~ pyopencl.enqueue_fill_buffer(self.queue, self.mask_already_tested_cl, np.zeros(1, dtype='uint8'), 0, self.mask_already_tested.size)
        #~ print('yop')
        #  TODO  max_wg_size
        #~ print('yep', self.mask_already_tested.shape)
        self.mask_already_tested[:] = 0
        event = pyopencl.enqueue_copy(self.queue,  self.mask_already_tested_cl, self.mask_already_tested)
        #~ print('yop')
        
        gsize = self.fifo_size - (2 * self.n_span)
        if gsize > self.max_wg_size:
            n = int(np.ceil(gsize / self.max_wg_size))
            global_size =  (self.max_wg_size * n, )
            local_size = (self.max_wg_size,)
        else:
            global_size = (gsize, )
            local_size = (gsize, )
        #~ print('global_size', global_size, 'local_size', local_size)
        
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_detect_local_peaks, global_size, local_size,)

        #~ if self._plot_debug:
            #~ pyopencl.enqueue_copy(self.queue,  self.fifo_residuals, self.fifo_residuals_cl)
            #~ pyopencl.enqueue_copy(self.queue,  self.nb_pending_peaks, self.nb_pending_peaks_cl)
            #~ pyopencl.enqueue_copy(self.queue,  self.pending_peaks, self.pending_peaks_cl)
            #~ fig, ax = plt.subplots()
            #~ ax.plot(self.fifo_residuals)
            #~ plt.show()
        
        
    
    def classify_and_align_next_spike(self):
        if self._plot_debug:
            print()
            print('classify_and_align_next_spike')
        
        #~ n = max(self.maximum_jitter_shift*2+1, self.nb_cluster)
        #~ print('n', n, 'self.nb_cluster', self.nb_cluster)
        
        #~ global_size = (self.nb_channel, 1)
        #~ local_size = (self.nb_channel, 1)
        #~ global_size = (n, self.nb_channel)
        #~ local_size = (n, 1)
        #~ local_size = (1, self.nb_channel)
        #~ local_size = (n, self.nb_channel)
        
        #~ print('self.max_wg_size', self.max_wg_size)
        #~ print('nb_channel', self.nb_channel)
        #~ print('n', n)
        # TODO self.max_wg_size
        
        #~ print('global_size', global_size, 'local_size', local_size)
        
        #~ event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_classify_and_align_next_spike, global_size, local_size,)
        
        
        
        global_size = (1, )
        local_size = (1, )
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_select_next_peak, global_size, local_size,)
        
        #TODO play with this!!!!!! local_size is very important for memory fence
        
        global_size = (self.nb_cluster, self.nb_channel)
        #~ local_size = (1, self.nb_channel)
        local_size = (self.nb_cluster, 1)   ####  comme gemotrical
        #~ local_size = (self.nb_cluster, self.nb_channel)
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_get_template_distances, global_size, local_size,)
        
        # TODO ajouter un kernel pour argmin  ICI pour armin distances
        
        global_size = (self.nb_shift, self.nb_channel)
        #~ local_size = (1, self.nb_channel)    ### marche pas
        local_size = (self.nb_shift, 1)  ####  comme gemotrical
        #~ local_size = (self.nb_shift, self.nb_channel)  ## marche mais trop gros dans certain cas
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_explore_shifts, global_size, local_size,)
        
        

        global_size = (1, )
        local_size = (1, )
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_finalize_next_spike, global_size, local_size,)
        
        event = pyopencl.enqueue_copy(self.queue,  self.next_spike, self.next_spike_cl)
        
        
        
        
        if self._plot_debug:
            event = pyopencl.enqueue_copy(self.queue,  self.next_peak, self.next_peak_cl)
            print(self.next_peak)
            print(self.next_spike)
        
        #~ exit()
        
        cluster_idx = self.next_spike[0]['cluster_idx']
        if self.next_spike[0]['cluster_idx'] >= 0:
            label = self.catalogue['cluster_labels'][cluster_idx]
        else:
            label = cluster_idx
        
        
        if self._plot_debug:
            print(Spike(self.next_spike[0]['peak_index'], label, self.next_spike[0]['jitter']))
        return Spike(self.next_spike[0]['peak_index'], label, self.next_spike[0]['jitter'])
        
        
    def reset_to_not_tested(self, good_spikes):
        #~ print('reset_to_not_tested')
        #~ print(good_spikes)
        
        # TODO self.max_wg_size
        global_size = (len(good_spikes), )
        local_size = (len(good_spikes), )
        
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_reset_tested_zone, global_size, local_size,)
        
        # mask_already_tested must NOT be reset here!!!

        gsize = self.fifo_size - (2 * self.n_span)
        if gsize > self.max_wg_size:
            n = int(np.ceil(gsize / self.max_wg_size))
            global_size =  (self.max_wg_size * n, )
            local_size = (self.max_wg_size,)
        else:
            global_size = (gsize, )
            local_size = (gsize, )
        #~ print('global_size', global_size, 'local_size', local_size)
        
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_detect_local_peaks, global_size, local_size,)
    
    
    def get_no_label_peaks(self):
        raise NotImplementedError
        # TODO
        #~ gsize = self.fifo_size - (2 * self.n_span)
        #~ if gsize > self.max_wg_size:
            #~ n = int(np.ceil(gsize / self.max_wg_size))
            #~ global_size =  (self.max_wg_size * n, )
            #~ local_size = (self.max_wg_size,)
        #~ else:
            #~ global_size = (gsize, )
            #~ local_size = (gsize, )
        
        #~ event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_detect_local_peaks, global_size, local_size,)
        
        #~ nolabel_indexes += self.n_span
        #~ nolabel_indexes = nolabel_indexes[nolabel_indexes<(self.chunksize+self.n_span)]
        #~ bad_spikes = np.zeros(0, dtype=_dtype_spike)
        #~ bad_spikes['index'] = nolabel_indexes
        #~ bad_spikes['cluster_label'] = LABEL_UNCLASSIFIED
        #~ return bad_spikes
        
    
    def _plot_before_peeling_loop(self):
        pyopencl.enqueue_copy(self.queue,  self.fifo_residuals, self.fifo_residuals_cl)
        self._plot_sigs_before = self.fifo_residuals.copy()

        pyopencl.enqueue_copy(self.queue,  self.nb_pending_peaks, self.nb_pending_peaks_cl)
        pyopencl.enqueue_copy(self.queue,  self.pending_peaks, self.pending_peaks_cl)
        pending_peaks =  self.pending_peaks[:self.nb_pending_peaks[0]]
        peak_inds = pending_peaks['peak_index']
        chan_inds = pending_peaks['peak_chan']
        print(pending_peaks)

        fig, ax = plt.subplots()
        
        plot_sigs = self.fifo_residuals.copy()
        self._plot_sigs_before = plot_sigs
        
        for c in range(self.nb_channel):
            plot_sigs[:, c] += c*30
        
        ax.plot(plot_sigs, color='k')

        ax.axvline(self.fifo_size - self.n_right, color='r')
        ax.axvline(-self.n_left, color='r')

        ax.scatter(peak_inds, plot_sigs[peak_inds, chan_inds], color='r')
    
    def _plot_after_peeling_loop(self, good_spikes):
        pyopencl.enqueue_copy(self.queue,  self.fifo_residuals, self.fifo_residuals_cl)
        
        self.mask_already_tested[:] = 0
        event = pyopencl.enqueue_copy(self.queue,  self.mask_already_tested_cl, self.mask_already_tested)

        gsize = self.fifo_size - (2 * self.n_span)
        if gsize > self.max_wg_size:
            n = int(np.ceil(gsize / self.max_wg_size))
            global_size =  (self.max_wg_size * n, )
            local_size = (self.max_wg_size,)
        else:
            global_size = (gsize, )
            local_size = (gsize, )
        event = pyopencl.enqueue_nd_range_kernel(self.queue,  self.kern_detect_local_peaks, global_size, local_size,)
        pyopencl.enqueue_copy(self.queue,  self.nb_pending_peaks, self.nb_pending_peaks_cl)
        pyopencl.enqueue_copy(self.queue,  self.pending_peaks, self.pending_peaks_cl)
        pending_peaks =  self.pending_peaks[:self.nb_pending_peaks[0]]
        peak_inds = pending_peaks['peak_index']
        chan_inds = pending_peaks['peak_chan']
        
        fig, ax = plt.subplots()
        #~ plot_sigs = self.fifo_residuals.copy()
        plot_sigs = self._plot_sigs_before
        
        
        #~ for c in range(self.nb_channel):
            #~ plot_sigs[:, c] += c*30
        #~ ax.plot(plot_sigs, color='k')
        
        ax.plot(self._plot_sigs_before, color='b')
        
        ax.axvline(self.fifo_size - self.n_right, color='r')
        ax.axvline(-self.n_left, color='r')

        ax.scatter(peak_inds, plot_sigs[peak_inds, chan_inds], color='r')
        
        
        
        good_spikes = np.array(good_spikes, dtype=_dtype_spike)
        pred = make_prediction_signals(good_spikes, self.internal_dtype, plot_sigs.shape, self.catalogue, safe=True)
        plot_pred = pred.copy()
        for c in range(self.nb_channel):
            plot_pred[:, c] += c*30
        
        ax.plot(plot_pred, color='m')
        
        plt.show()

        
    def _plot_after_inner_peeling_loop(self):
        return
        #~ pyopencl.enqueue_copy(self.queue,  self.nb_pending_peaks, self.nb_pending_peaks_cl)
        #~ pyopencl.enqueue_copy(self.queue,  self.pending_peaks, self.pending_peaks_cl)
        pyopencl.enqueue_copy(self.queue,  self.fifo_residuals, self.fifo_residuals_cl)



        
        #~ self._plot_sigs_before = plot_sigs
        #~ chan_order = np.argsort(self.channel_distances[0, :])
        
        fig, ax = plt.subplots()
        
        plot_sigs = self._plot_sigs_before.copy()
        
        for c in range(self.nb_channel):
            plot_sigs[:, c] += c*30
        ax.plot(plot_sigs, color='b')
        
        plot_sigs = self.fifo_residuals.copy()
        
        
        for c in range(self.nb_channel):
            plot_sigs[:, c] += c*30
        
        ax.plot(plot_sigs, color='k')

        ax.axvline(self.fifo_size - self.n_right, color='r')
        ax.axvline(-self.n_left, color='r')

        #~ mask = self.peakdetector.get_mask_peaks_in_chunk(self.fifo_residuals)
        #~ peak_inds, chan_inds= np.nonzero(mask)
        #~ peak_inds += self.n_span
        
        #~ ax.scatter(peak_inds, plot_sigs[peak_inds, chan_inds], color='r')
        #~ ax.plot(self.fifo_residuals)
        plt.show()
        
        self._plot_sigs_before = self.fifo_residuals.copy()




kernel_peeler_cl = """
#define chunksize %(chunksize)d
#define n_span %(n_span)d
#define nb_channel %(nb_channel)d
#define nb_cluster %(nb_cluster)d
#define relative_threshold %(relative_threshold)d
#define peak_sign %(peak_sign)d
#define extra_size %(extra_size)d
#define fifo_size %(fifo_size)d
#define n_left %(n_left)d
#define n_right %(n_right)d
#define peak_width %(peak_width)d
#define maximum_jitter_shift %(maximum_jitter_shift)d
#define n_cluster %(n_cluster)d
#define wf_size %(wf_size)d
#define subsample_ratio %(subsample_ratio)d
#define nb_neighbour %(nb_neighbour)d
#define inter_sample_oversampling %(inter_sample_oversampling)d
#define nb_shift %(nb_shift)d



#define LABEL_LEFT_LIMIT -11
#define LABEL_RIGHT_LIMIT -12
#define LABEL_MAXIMUM_SHIFT -13
#define LABEL_TRASH -1
#define LABEL_NOISE -2
#define LABEL_ALIEN -9
#define LABEL_UNCLASSIFIED -10
#define LABEL_NO_WAVEFORM -11
#define LABEL_NOT_YET 0
#define LABEL_NO_MORE_PEAK -20


typedef struct st_spike{
    int peak_index;
    int cluster_idx;
    float jitter;
} st_spike;

typedef struct st_peak{
    int peak_index;
    int peak_chan;
    float peak_value;
} st_peak;



__kernel void add_fifo_residuals(__global  float *fifo_residuals, __global  float *sigs_chunk, int fifo_roll_size){
    int pos = get_global_id(0);
    int chan = get_global_id(1);
    
    //work on ly for n<chunksize
    if (pos<fifo_roll_size){
        fifo_residuals[pos*nb_channel+chan] = fifo_residuals[(pos+chunksize)*nb_channel+chan];
    }
    barrier(CLK_GLOBAL_MEM_FENCE);
    
    fifo_residuals[(pos+fifo_roll_size)*nb_channel+chan] = sigs_chunk[pos*nb_channel+chan];
}


inline void atomic_add_float(volatile __global float *source, const float operand) {
    union {
        unsigned int intVal;
        float floatVal;
    } newVal;
    union {
        unsigned int intVal;
        float floatVal;
    } prevVal;
    do {
        prevVal.floatVal = *source;
        newVal.floatVal = prevVal.floatVal + operand;
    } while (atomic_cmpxchg((volatile __global unsigned int *)source, prevVal.intVal, newVal.intVal) != prevVal.intVal);
}



__kernel void detect_local_peaks(
                        __global  float *sigs,
                        __global  int *neighbours,
                        __global  uchar *mask_already_tested,
                        __global  st_peak *pending_peaks,
                        volatile __global int *nb_pending_peaks
                ){
    int pos = get_global_id(0);
    

    
    if (pos == 0){
        *nb_pending_peaks = 0;
    }
    barrier(CLK_GLOBAL_MEM_FENCE);

    if (pos>=(fifo_size - (2 * n_span))){
        return;
    }
    

    float v;
    uchar peak;
    int chan_neigh;
    
    int i_peak;

    
    for (int chan=0; chan<nb_channel; chan++){
    
        v = sigs[(pos + n_span)*nb_channel + chan];

    
        if(peak_sign==1){
            if (v>relative_threshold){peak=1;}
            else if (v<=relative_threshold){peak=0;}
        }
        else if(peak_sign==-1){
            if (v<-relative_threshold){peak=1;}
            else if (v>=-relative_threshold){peak=0;}
        }
        
        // avoid peak already tested
        if (mask_already_tested[(pos + n_span)*nb_channel + chan] == 1){
            peak = 0;
        }
        
        if (peak == 1){
            for (int neighbour=0; neighbour<nb_neighbour; neighbour++){
                // when neighbour then chan==chan_neigh (itself)
                chan_neigh = neighbours[chan * nb_neighbour +neighbour];
                
                if (chan_neigh<0){continue;}
                
                if (chan != chan_neigh){
                    if(peak_sign==1){
                        peak = peak && (v>=sigs[(pos + n_span)*nb_channel + chan_neigh]);
                    }
                    else if(peak_sign==-1){
                        peak = peak && (v<=sigs[(pos + n_span)*nb_channel + chan_neigh]);
                    }
                }
                
                if (peak==0){break;}
                
                if(peak_sign==1){
                    for (int i=1; i<=n_span; i++){
                        peak = peak && (v>sigs[(pos + n_span - i)*nb_channel + chan_neigh]) && (v>=sigs[(pos + n_span + i)*nb_channel + chan_neigh]);
                        if (peak==0){break;}
                    }
                }
                else if(peak_sign==-1){
                    for (int i=1; i<=n_span; i++){
                        peak = peak && (v<sigs[(pos + n_span - i)*nb_channel + chan_neigh]) && (v<=sigs[(pos + n_span + i)*nb_channel + chan_neigh]);
                        if (peak==0){break;}
                    }
                }
            
            }
            
        }
        
        if (peak==1){
            //append to 
            i_peak = atomic_inc(nb_pending_peaks);
            // peak_index is LOCAL to fifo
            pending_peaks[i_peak].peak_index = pos + n_span;
            pending_peaks[i_peak].peak_chan = chan;
            pending_peaks[i_peak].peak_value = fabs(v);
        }
    }
    
}



__kernel void select_next_peak(
                        __global float *fifo_residuals,
                        __global st_peak *next_peak,
                        __global  st_peak *pending_peaks,
                        __global int *nb_pending_peaks,
                        __global st_spike *next_spike,
                        float adjacency_radius_um,
                        float alien_value_threshold
                        ){
    
    // take max amplitude
    
    int i_peak = -1;
    float best_value = 0.0;
    
    for (int i=0; i<*nb_pending_peaks; i++){
        if ((pending_peaks[i].peak_value > best_value)){
            i_peak = i;
            best_value = pending_peaks[i].peak_value;
        }
    }
    
    if (i_peak == -1){
        next_peak->peak_index = LABEL_NO_MORE_PEAK;
        next_peak->peak_chan = -1;
        next_peak->peak_value = 0.0;
        next_spike->peak_index = LABEL_NO_MORE_PEAK;
        next_spike->cluster_idx = LABEL_NO_MORE_PEAK;
        next_spike->jitter = 0.0f;
    }else{
        
        st_peak peak;
        
        peak = pending_peaks[i_peak];
        *next_peak = peak;

        // make this peak not selectable for next choice
        pending_peaks[i_peak].peak_value = 0.0;
        
        
        int left_ind = peak.peak_index + n_left;
        
        if (left_ind+peak_width+maximum_jitter_shift+1>=fifo_size){
            next_spike->peak_index = peak.peak_index;
            next_spike->cluster_idx = LABEL_RIGHT_LIMIT;
            next_spike->jitter = 0.0f;
        } else if (left_ind<=maximum_jitter_shift){
            next_spike->peak_index = peak.peak_index;
            next_spike->cluster_idx = LABEL_LEFT_LIMIT;
            next_spike->jitter = 0.0f;
        } else if (n_cluster==0){
            next_spike->cluster_idx  = LABEL_UNCLASSIFIED;
        }else {
            next_spike->cluster_idx = LABEL_NOT_YET;
            if (alien_value_threshold>0){
                for (int s=0; s<peak_width; ++s){
                    if ((fifo_residuals[(left_ind+s)*nb_channel + peak.peak_chan]>alien_value_threshold) || (fifo_residuals[(left_ind+s)*nb_channel + peak.peak_chan]<-alien_value_threshold)){
                        next_spike->cluster_idx = LABEL_ALIEN;
                        next_spike->jitter = 0.0f;
                        next_spike->peak_index = peak.peak_index;
                    }
                }
            }
        }    
    }
}


float estimate_one_jitter(int left_ind, int cluster_idx,
                                        __global int *extremum_channel,
                                        __global float *fifo_residuals,
                                        
                                        __global float *catalogue_center0,
                                        __global float *catalogue_center1,
                                        __global float *catalogue_center2,
                                        
                                        __global float *wf1_norm2,
                                        __global float *wf2_norm2,
                                        __global float *wf1_dot_wf2){

    int chan_max;
    float jitter;
        
    chan_max= extremum_channel[cluster_idx];
    
    
    float h, wf1, wf2;
    float h0_norm2 = 0.0f;
    float h_dot_wf1 =0.0f;
    
    for (int s=0; s<peak_width; ++s){
        h = fifo_residuals[(left_ind+s)*nb_channel + chan_max] - catalogue_center0[wf_size*cluster_idx+nb_channel*s+chan_max];
        h0_norm2 +=  h*h;
        wf1 = catalogue_center1[wf_size*cluster_idx+nb_channel*s+chan_max];
        h_dot_wf1 += h * wf1;
        
    }
    
    jitter = h_dot_wf1/wf1_norm2[cluster_idx];
    
    float h1_norm2;
    h1_norm2 = 0.0f;
    for (int s=0; s<peak_width; ++s){
        h = fifo_residuals[(left_ind+s)*nb_channel + chan_max] - catalogue_center0[wf_size*cluster_idx+nb_channel*s+chan_max];
        wf1 = catalogue_center1[wf_size*cluster_idx+nb_channel*s+chan_max];
        h1_norm2 += (h - jitter * wf1) * (h - jitter * wf1);
    }
    
    if (h0_norm2 > h1_norm2){
        float h_dot_wf2 =0.0f;
        float rss_first, rss_second;
        for (int s=0; s<peak_width; ++s){
            h = fifo_residuals[(left_ind+s)*nb_channel + chan_max] - catalogue_center0[wf_size*cluster_idx+nb_channel*s+chan_max];
            wf2 = catalogue_center2[wf_size*cluster_idx+nb_channel*s+chan_max];
            h_dot_wf2 += h * wf2;
        }
        rss_first = -2*h_dot_wf1 + 2*jitter*(wf1_norm2[cluster_idx] - h_dot_wf2) + pown(3*jitter, 2)*wf1_dot_wf2[cluster_idx] + pown(jitter,3)*wf2_norm2[cluster_idx];
        rss_second = 2*(wf1_norm2[cluster_idx] - h_dot_wf2) + 6*jitter*wf1_dot_wf2[cluster_idx] + 3*pown(jitter, 2) * wf2_norm2[cluster_idx];
        jitter = jitter - rss_first/rss_second;
    } else{
        jitter = 0.0f;
    }
    
    return jitter;
}

int accept_tempate(int left_ind, int cluster_idx, float jitter,
                                        __global float *fifo_residuals,
                                        __global uchar *sparse_mask,
                                        __global float *catalogue_center0,
                                        __global float *catalogue_center1,
                                        __global float *catalogue_center2,
                                        __global float *weight_per_template){


    if (fabs(jitter) > (maximum_jitter_shift - 0.5)){
        return 0;
    }

    float v;
    float pred;
    
    float chan_in_mask = 0.0f;
    float chan_with_criteria = 0.0f;
    int idx;
    
    float w;

    
    for (int c=0; c<nb_channel; ++c){
        if (sparse_mask[cluster_idx*nb_channel + c] == 1){

            float wf_nrj = 0.0f;
            float res_nrj = 0.0f;
        
            for (int s=0; s<peak_width; ++s){
                v = fifo_residuals[(left_ind+s)*nb_channel + c];
                wf_nrj += (v*v);
                
                idx = wf_size*cluster_idx+nb_channel*s+c;
                pred = catalogue_center0[idx] + jitter*catalogue_center1[idx] + jitter*jitter/2*catalogue_center2[idx];
                v -= pred;
                res_nrj += (v*v);
            }
            
            w = weight_per_template[cluster_idx*nb_channel + c];
            chan_in_mask += w;
            if (wf_nrj>res_nrj){
                chan_with_criteria += w;
            }
        }
    }
    
    if (chan_with_criteria>=(chan_in_mask*0.7f)){
        return 1;
    }else{
        return 0;
    }

}



__kernel void get_template_distances(__global  float *fifo_residuals,
                                                                __global st_spike *next_spike,
                                                                __global  st_peak *next_peak,

                                                                __global  float *catalogue_center0,
                                                                
                                                                __global  uchar  *high_sparse_mask,
                                                                __global  float *waveform_distance,
                                                                
                                                                __global float *channel_distances,
                                                                float adjacency_radius_um
                                                                ){

    int cluster_idx = get_global_id(0);
    int chan = get_global_id(1);

    if (cluster_idx>=nb_cluster){return;}
    if (chan>=nb_channel){return;}    
    
    // each worker
    int left_ind;
    st_spike spike;
    st_peak peak;
    
    spike = *next_spike;
    
    
    if (spike.cluster_idx==LABEL_NOT_YET) {
        int ok;
        
        peak = *next_peak;
        
        left_ind = peak.peak_index + n_left;
    

        // initialize distance
        //parralel on cluster_idx
        if (chan==0){
            for (int cluster_idx=0; cluster_idx<nb_cluster; ++cluster_idx){
                // the peak_chan do not overlap spatialy this cluster
                if (high_sparse_mask[nb_channel*cluster_idx+peak.peak_chan] == 0){
                    waveform_distance[cluster_idx] = FLT_MAX;
                }
                else {
                    // candidate initialize sum by cluster
                    waveform_distance[cluster_idx] = 0.0f;
                }
            }
        }
        barrier(CLK_GLOBAL_MEM_FENCE);
        
        // compute distance
        if (high_sparse_mask[nb_channel*cluster_idx+peak.peak_chan] == 1){
            if (channel_distances[chan * nb_channel + peak.peak_chan] < adjacency_radius_um){
                float sum = 0;
                float d;
                for (int s=0; s<peak_width; ++s){
                    d =  fifo_residuals[(left_ind+s)*nb_channel + chan] - catalogue_center0[wf_size*cluster_idx+nb_channel*s+chan];
                    sum += d*d;
                }
                atomic_add_float(&waveform_distance[cluster_idx], sum);
            }
        }
    }
}
        
__kernel void explore_shifts(__global  float *fifo_residuals,
                                            __global st_spike *next_spike,
                                            __global  st_peak *next_peak,

                                            __global  float *catalogue_center0,
                                            __global  uchar  *sparse_mask,
                                            __global  float *waveform_distance,
                                            __global  float *waveform_distance_shifts
                                        ){

    int shift_ind = get_global_id(0);
    int chan = get_global_id(1);
    
    if (shift_ind>=nb_shift){return;}
    if (chan>=nb_channel){return;}
    
    
    st_spike spike;
    spike = *next_spike;
    
    if (spike.cluster_idx==LABEL_NOT_YET) {
        st_peak peak;
        int left_ind;

        
        peak = *next_peak;
        left_ind = peak.peak_index + n_left;
    
    
        // argmin on distance
        // not parralel zone only first worker
        if ((chan==0) && (shift_ind==0)){
            //argmin  not paralel
            float min_dist;
            min_dist = MAXFLOAT;
            spike.cluster_idx = LABEL_UNCLASSIFIED;
            for (int clus=0; clus<n_cluster; ++clus){
                if (waveform_distance[clus]<min_dist){
                    spike.cluster_idx = clus;
                    min_dist = waveform_distance[clus];
                }
            }
            *next_spike = spike;
        }
        barrier(CLK_GLOBAL_MEM_FENCE);
        spike = *next_spike;
        
        // can be LABEL_UNCLASSIFIED when no cluster
        // have sparse mask on channel
        if (spike.cluster_idx>=0){
        
            // explore shifts
            if (chan==0){
                waveform_distance_shifts[shift_ind] = 0.0f;
            }
            barrier(CLK_GLOBAL_MEM_FENCE);

            if (sparse_mask[nb_channel*spike.cluster_idx + chan] == 1){
                int shift;
                shift = shift_ind - maximum_jitter_shift;
                float sum = 0;
                float d;
                for (int s=0; s<peak_width; ++s){
                    d =  fifo_residuals[(left_ind+s+shift)*nb_channel + chan] - catalogue_center0[wf_size*spike.cluster_idx+nb_channel*s+chan];
                    sum += d*d;
                }
                atomic_add_float(&waveform_distance_shifts[shift_ind], sum);
            }
        }
    }
}

    
    

__kernel void finalize_next_spike(
                                            __global  float *fifo_residuals,
                                            __global st_spike *next_spike,
                                            __global  st_peak *next_peak,
                                            __global  st_peak *pending_peaks,
                                            __global int *nb_pending_peaks,
                                            
                                            __global  st_spike *good_spikes,
                                            __global int *nb_good_spikes,
                                            
                                            
                                            
                                            __global  uchar *mask_already_tested,
                                            __global  float *catalogue_center0,
                                            __global  float *catalogue_center1,
                                            __global  float *catalogue_center2,
                                            __global  float *catalogue_inter_center0,
                                            __global  uchar  *sparse_mask,
                                            __global  float *waveform_distance_shifts,
                                            __global int *extremum_channel,
                                            __global float *wf1_norm2,
                                            __global float *wf2_norm2,
                                            __global float *wf1_dot_wf2,
                                            __global float *weight_per_template
                                            ){
    
    st_spike spike;
    spike = *next_spike;
    
    if (spike.cluster_idx>=0) {
        
        st_peak peak;
        int left_ind;

        
        peak = *next_peak;
        left_ind = peak.peak_index + n_left;

        int shift = 0;
        // argmin for shifts
        float min_dist = MAXFLOAT;
        for (int shift_ind=0; shift_ind<(2 * maximum_jitter_shift + 1); ++shift_ind){
            if (waveform_distance_shifts[shift_ind]<min_dist){
                shift = shift_ind - maximum_jitter_shift;
                min_dist = waveform_distance_shifts[shift_ind];
            }
        }
        //DebUG
        //shift = 0;
        
        left_ind = left_ind + shift;
    
        // estimate jitter
        float jitter;
        float new_jitter;
        
        if (inter_sample_oversampling){
            jitter = estimate_one_jitter(left_ind, spike.cluster_idx,
                                    extremum_channel, fifo_residuals,
                                    catalogue_center0, catalogue_center1, catalogue_center2,
                                    wf1_norm2, wf2_norm2, wf1_dot_wf2);
        } else {
            jitter = 0.0f;
        }
        
        int ok;
        // accept template
        ok = accept_tempate(left_ind, spike.cluster_idx, jitter,
                                    fifo_residuals, sparse_mask,
                                    catalogue_center0, catalogue_center1, catalogue_center2,
                                    weight_per_template);
        
        //debug
        //ok = 1;
        
        if (ok == 0){
            //spike.jitter = 0.0f;
            //spike.jitter = (float) spike.cluster_idx;
            spike.jitter = (float) shift;
            //spike.jitter = waveform_distance_shifts[5];
            //spike.jitter = waveform_distance[3];
            //spike.jitter = jitter;
            spike.cluster_idx = LABEL_UNCLASSIFIED;
        } else {
            // test when jitter is more than one sample
            shift = - ((int) round(jitter));
            if (inter_sample_oversampling && (fabs(jitter) > 0.5f) && ((left_ind+shift+peak_width)<fifo_size) && ((left_ind + shift) >= 0) ){
                int ok2;
                new_jitter = estimate_one_jitter(left_ind+shift, spike.cluster_idx,
                                                    extremum_channel, fifo_residuals,
                                                    catalogue_center0, catalogue_center1, catalogue_center2,
                                                    wf1_norm2, wf2_norm2, wf1_dot_wf2);
                ok2 = accept_tempate(left_ind+shift, spike.cluster_idx, new_jitter,
                                                    fifo_residuals, sparse_mask,
                                                    catalogue_center0, catalogue_center1, catalogue_center2,
                                                    weight_per_template);
                if ((fabs(new_jitter)<fabs(jitter)) && (ok2==1)){
                    jitter = new_jitter;
                    left_ind = left_ind + shift;
                }
            }
            
            if (abs(shift) >maximum_jitter_shift){
                spike.cluster_idx = LABEL_MAXIMUM_SHIFT;
            } else if ((left_ind+shift+peak_width) >= fifo_size){
                spike.cluster_idx = LABEL_RIGHT_LIMIT;
            } else if ((left_ind+shift)<0) {
                spike.cluster_idx = LABEL_LEFT_LIMIT;
            }
        }

        // security check for borders
        if (spike.cluster_idx >= 0){
            int left_ind_check;
            left_ind_check = left_ind - ((int) round(jitter));
            if (left_ind_check < 0){
                spike.cluster_idx = LABEL_LEFT_LIMIT;
            } else if ((left_ind_check+peak_width) >= fifo_size){
                spike.cluster_idx = LABEL_RIGHT_LIMIT;
            }
        }
        
        
        // final
        if (spike.cluster_idx < 0){
            spike.peak_index = peak.peak_index;
            
            // set already tested
            mask_already_tested[peak.peak_index * nb_channel + peak.peak_chan] =1;
            
        } else{
            shift = - ((int) round(jitter));
            if (shift !=0){
                jitter = jitter + shift;
                left_ind = left_ind + shift;
            }
            spike.jitter = jitter;
            spike.peak_index = left_ind - n_left;
            
            // on_accepted_spike
            int int_jitter;
            
            // remove prediction from residual
            int_jitter = (int) ((jitter) * subsample_ratio) + (subsample_ratio / 2 );
            for (int s=0; s<peak_width; ++s){
                for (int c=0; c<nb_channel; ++c){
                    fifo_residuals[(left_ind+s)*nb_channel + c] -= catalogue_inter_center0[subsample_ratio*wf_size*spike.cluster_idx + nb_channel*(s*subsample_ratio+int_jitter) + c];
                }
            }
            
            // set_already_tested_spike_zone = remove spike in zone in pendings peaks
            for (int i=0; i<*nb_pending_peaks; i++){
                if (pending_peaks[i].peak_value > 0.0){
                    if (
                            (pending_peaks[i].peak_index  > (spike.peak_index+ n_left)) && 
                            (pending_peaks[i].peak_index < (spike.peak_index+ n_right)) && 
                            (sparse_mask[spike.cluster_idx * nb_channel + pending_peaks[i].peak_chan] == 1) ){
                        pending_peaks[i].peak_value = 0;
                    }
                }
            }
            
            //add spike to good_spike stack
            good_spikes[*nb_good_spikes] = spike;
            *nb_good_spikes = *nb_good_spikes + 1;
            
            
        }
        
        *next_spike = spike ;
    }

}


__kernel void reset_tested_zone(__global  uchar *mask_already_tested,
                __global st_spike *good_spikes,
                __global int *nb_good_spikes,
                __global uchar *sparse_mask
                ){
    
    int n = get_global_id(0);
    
    // reset tested zone around each spike
    for (int c=0; c<nb_channel; ++c){
        if (sparse_mask[good_spikes[n].cluster_idx * nb_channel + c]==1){
            
            
            //for (int s=-peak_width; s<=peak_width; ++s){
            // TODO study this case 
            for (int s=n_left; s<=n_right; ++s){
                mask_already_tested[(good_spikes[n].peak_index + s) * nb_channel + c] = 0;
            }
        }
    }
    
    if (n == 0){
        *nb_good_spikes = 0;
    }
}





"""




