import time, sys, itertools
from tqdm.auto import tqdm, trange

import pandas as pd
import numpy as np
from scipy.sparse import coo_matrix

from scipy.ndimage import gaussian_filter

# Include VC / VC_SQRT norm
class NormalizerBin:
	def __init__(self, method='SQVC'):
		self.method = method
		self.vec = None

	def fit(self, bulk, eps=1e-10):
		row_sum = np.array(bulk.sum(0), copy=False).ravel() + eps
		if self.method == 'SQVC':
			self.vec = row_sum ** (-.5)
		elif self.method == 'VC':
			self.vec = row_sum ** -1
		else:
			raise NotImplementedError
		return self

	def transform(self, m, inplace=True):
		assert inplace
		if type(m).__name__ == 'coo_matrix':
			total = m.data.sum()
			m.data *= self.vec[m.col] * self.vec[m.row]
			m.data *= total / m.data.sum()
		elif type(m).__name__ == 'ndarray':
			total = m.sum()
			m *= self.vec[None, :] * self.vec[:, None]
			m *= total / m.sum()
		return m


class NormalizerOE:
	def __init__(self):
		self.vec = None

	def fit(self, bulk):
		L = len(bulk)
		raise NotImplementedError
		vec = bulk.ravel()[:-1].reshape(L-1, L+1)[:, :-1].sum(0)
		vec[0] += bulk[-1, -1]
		self.vec = vec ** -1
		return self

	def transform(self, m, inplace=True):
		assert type(m).__name__ == 'coo_matrix'
		assert inplace
		total = m.data.sum()
		m.data *= self.vec[np.abs(m.col - m.row)]
		m.data *= total / m.data.sum()
		return m


class Clip:
	def __init__(self, axis='entry', s=10.):
		self.thr = None
		self.axis = axis
		self.s = s

	def fit(self, matrix_list, bulk):
		bulk_0 = calc_bulk([coo_matrix((np.ones_like(m.data), (m.row, m.col)), shape=m.shape) for m in matrix_list])
		bulk_0 += 1e-10
		bulk_2 = calc_bulk([coo_matrix((m.data**2, (m.row, m.col)), shape=m.shape) for m in matrix_list])
		if self.axis == 'entry':
			bulk_1 = bulk / bulk_0
			bulk_2 /= bulk_0
		elif self.axis == 'row':
			bulk_1 = bulk.sum(1, keepdims=True) / bulk_0.sum(1, keepdims=True)
			bulk_2 = bulk_2.sum(1, keepdims=True) / bulk_0.sum(1, keepdims=True)
		else: raise NotImplementedError
		mean = bulk_1
		std = bulk_2 - bulk_1**2
		assert (std >= -1e-5).all(), std.min()
		std = np.maximum(0, std) ** .5
		self.thr = np.broadcast_to(mean + std * self.s, shape=bulk.shape)

	def transform(self, m, inplace=True):
		assert inplace
		if isinstance(m, np.ndarray):
			m[:] = np.clip(m.data, a_min=None, a_max=self.thr)
		else:
			thr = self.thr[m.row, m.col]
			cnt = m.data > thr
			m.data = np.clip(m.data, a_min=None, a_max=thr)
			assert (m.data > 0).all()
		return m


def regress_out(x, y):
	# y = np.unique(y, return_inverse=True)[1]
	mean = pd.DataFrame(x).groupby(y).mean()
	x = x - mean.loc[y].values
	return x


def quantile_normalization(x, y):
	y = np.unique(y, return_inverse=True)[1]
	z = np.empty_like(x)
	for c, df in pd.DataFrame(x).groupby(y):
		rank_mean = df.stack().groupby(df.rank(method='first').stack().astype(int)).mean()
		df = df.rank(method='min').stack().astype(int).map(rank_mean).unstack()
		z[df.index] = df.values
	return z


def calc_bulk(matrix_list):
	shape = matrix_list[0].shape
	nnz = sum(m.nnz for m in matrix_list)
	indices = np.empty([3, nnz], dtype=np.int16)
	values = np.empty([nnz], dtype=np.float32)
	del nnz
	idx_nnz = 0
	for i, m in enumerate(tqdm(matrix_list)):
		idx = slice(idx_nnz, idx_nnz + m.nnz)
		indices[0, idx] = m.row
		indices[1, idx] = m.col
		values[idx] = m.data
		idx_nnz += m.nnz
		del idx, m
	bulk = coo_matrix((values[:idx_nnz], tuple(indices[:2, :idx_nnz])), shape)
	del idx_nnz
	bulk = np.array(bulk.todense())
	bulk /= len(matrix_list)
	return bulk


def normalize_by_coverage(m, mi=None, scale=None):
	scale = m.shape[0] if scale is None else scale
	if m is mi or mi is None: n = m.sum()
	else: n = m.sum() + mi.sum()
	m.data *= scale / (n + 1e-15)
	return m


def normalize_by_coverage_clip(m, mi=None, scale=None, bulk=None):
	scale = m.shape[0] if scale is None else scale
	if m is mi or mi is None: n = m.sum()
	else: n = m.sum() + mi.sum()
	off_diag = (np.sum(m > 0) - np.sum(m.diagonal() > 0)) / 2
	if off_diag > m.shape[0]:
		m.data *= scale / (n + 1e-15)
	else:
		# print ("clip low cov_data")
		m.data *= scale / (n + 1e-15)
	return m

def conv(m, *args, **kwargs):
	A = gaussian_filter((m).astype(np.float32).toarray(), 1, order=0, mode='mirror', truncate=1)
	return A

def log1p_matrix(m):
	m.data = np.log1p(m.data)
	return m


def half_main_diag(m, *args, **kwargs):
	m.data[m.col == m.row] /= 2
	return m
def zero_main_diag(m, *args, **kwargs):
	m.data[m.col == m.row] = 0.0
	return m

def normalize_per_cell(
		matrix_list, matrix_list_intra, bulk=None, per_cell_normalize_func=(),
		normalizers=(),
):
	if bulk is None: bulk = calc_bulk(matrix_list)
	# normalizers = [
	# 	NormalizerBin(method='SQVC'),
	# 	NormalizerOE(),
	# ]
	for normalizer in normalizers:
		normalizer.fit(matrix_list=matrix_list, bulk=bulk)
		bulk = normalizer.transform(bulk)
	for m, mi in zip(matrix_list, matrix_list_intra):
		for normalizer in normalizers:
			normalizer.transform(m)
		for func in per_cell_normalize_func:
			m = func(m, mi)

	return matrix_list


def reformat_input(matrix_list, config, valid_bin=None, off_diag=None, fac_size=None, loss_distribution='Gaussian', sparse=False):
	m = matrix_list[0]
	if off_diag is None:
		off_diag = int(50000000 / config['resolution'])

	if fac_size is None:
		fac_size = int(300000 / config['resolution'])
	if fac_size <= 2:
		fac_size = 1

	if valid_bin is None: valid_bin = np.ones(m.shape[0], dtype=bool)

	nnz = sum(m.nnz for m in matrix_list)
	indices = np.empty([3, nnz], dtype=np.int16)
	values = np.empty([nnz], dtype=np.float32)
	del nnz

	patch_size = min(2 * off_diag + 1, m.shape[1])
	shape = (m.shape[0], patch_size)
	size_l = patch_size // 2
	size_r = (patch_size + 1) // 2
	idxs = np.mgrid[[slice(0, s) for s in [m.shape[0], patch_size]]]
	mask = (idxs[0] + idxs[1] >= size_l) & (idxs[0] + idxs[1] < sum(shape)-size_r)
	if sparse:
		new_a = None
	else:
		indices = None
		values = None
		new_a = np.empty(shape + (len(matrix_list),), dtype=np.float32)

	idx_nnz = 0
	for i, m in enumerate(tqdm(matrix_list)):
		# if loss_distribution in ['Gaussian', 'ZIG']:
		# 	pass
		# elif loss_distribution in ['NB']:
		# 	pass
		# else:
		# 	raise ValueError

		row, col, data = m.row, m.col, m.data
		col_new = col - row
		idx = valid_bin[row] & valid_bin[col] & (col_new >= -size_l) & (col_new < size_r)
		row, col, data = row[idx], col_new[idx], data[idx]
		col += size_l
		del col_new

		if sparse:
			nnz = len(data)
			ii = slice(idx_nnz, idx_nnz + nnz)
			indices[0, ii] = row
			indices[1, ii] = col
			indices[2, ii] = i
			values[ii] = data
			idx_nnz += nnz
			del nnz, ii
		else:
			new_a[..., i] = coo_matrix((data.astype(np.float32), (row, col)), shape=shape).todense()

	if sparse:
		return (
			np.ascontiguousarray(indices[:, :idx_nnz]),
			np.ascontiguousarray(values[:idx_nnz]),
			mask.shape + (len(matrix_list),)
		), mask
	else:
		return new_a, mask


def correct_batch_effect_pre(matrix_list, data_list):
	indices, values, shape = matrix_list
	if np.all(np.diff(indices[2]) >= 0):
		data_list = [0] + list(np.searchsorted(indices[2], [dl_slice.stop for dl_slice in data_list], side='right'))
		data_list = [slice(*_) for _ in zip(data_list[:-1], data_list[1:])]
	else: raise NotImplementedError
	bulks = [
		np.array(coo_matrix((values[slice_], tuple(indices[:2, slice_])), tuple(shape[:2])).todense())
		/ (slice_.stop - slice_.start)
		for slice_ in data_list
	]
	cols_avg = [bulk.mean(0) for bulk in bulks]
	rows_avg = [bulk.mean(1) for bulk in bulks]
	avg_func = lambda x: np.mean(x, 0)
	# def avg_func(x):
	# 	x = np.stack(x)
	# 	idx = x > 0
	# 	x[~idx] = 1
	# 	y = np.exp(np.log(x).sum(0) / idx.sum(0))
	# 	return y
	# col_avg = cols_avg[0]
	# row_avg = rows_avg[0]
	# col_avg = np.mean(cols_avg, axis=0)
	# row_avg = np.mean(rows_avg, axis=0)
	col_avg = avg_func(cols_avg)
	row_avg = avg_func(rows_avg)
	factors_col = [col_avg / avg for avg in cols_avg]
	factors_row = [row_avg / avg for avg in rows_avg]
	for slice_, factor_col, factor_row in zip(data_list, factors_col, factors_row):
		values[slice_] *= factor_col[indices[1, slice_]]
		# values[slice_] *= factor_row[indices[0, slice_]]
	assert not np.isnan(values).any()
	return matrix_list


def downsample(matrix_list, data_list, bulk_list=None, mode='stratum', rate_mode='minimum'):
	def slicing(a, i, tolist=True):
		if isinstance(i, slice): return a[i]
		ret = itertools.compress(a, i)
		if tolist: return list(ret)
		else: return ret
	if bulk_list is None: bulk_list = [calc_bulk(slicing(matrix_list, slc)) for slc in data_list]
	L = len(bulk_list[0])
	obs_list = []
	if mode == 'global':
		obs_list = [[bulk.sum()] for bulk in bulk_list]
	elif mode == 'stratum':
		for bulk in bulk_list:
			tmp = bulk.copy().ravel()[:-1].reshape(L-1, L+1)
			np.cumsum(tmp, axis=0, out=tmp)
			obs = np.empty(L)
			obs[1:] = tmp.ravel()[L-1::L][::-1]
			obs[0] = tmp[-1, 0] + bulk[-1, -1]
			assert np.isclose(obs[0], np.diag(bulk).sum(), atol=1e-2, rtol=1e-5)
			assert np.isclose(obs[1], np.diag(bulk, 1).sum(), atol=1e-2, rtol=1e-5)
			obs_list.append(obs)
			del tmp
	else: raise NotImplementedError
	obs_list = np.array(obs_list)
	if rate_mode == 'minimum':
		target = obs_list.copy()
		print(f'# of empty entries = {(target == 0).any(0).sum()}')
		sys.stdout.flush()
		target[target == 0] = np.nan
		target = np.nanmin(target, 0)
		assert not (target <= 0).any()
	else: raise NotImplementedError
	for slc, obs in zip(data_list, obs_list):
		rate = target / obs # It's ok to have nan, because these entries won't be used
		if (np.nan_to_num(rate, 1.) == 1).all(): continue
		assert (rate[obs > 0] > 0).all()
		assert (rate[obs > 0] <= 1).all()
		for matrix in tqdm(slicing(matrix_list, slc, tolist=False)):
			mask_u = matrix.row > matrix.col
			mask_l = matrix.row < matrix.col
			assert mask_u.sum() == mask_l.sum()
			mask = ~mask_l
			if mode == 'global': r = rate
			elif mode == 'stratum': r = rate[matrix.row[mask] - matrix.col[mask]]
			else: raise NotImplementedError
			matrix.data[mask] = np.random.binomial(matrix.data[mask].astype(int), r).astype(np.float32)
			matrix.col[mask_l] = matrix.row[mask_u]
			matrix.row[mask_l] = matrix.col[mask_u]
			matrix.data[mask_l] = matrix.data[mask_u]
			matrix.eliminate_zeros()
			assert not np.isnan(matrix.data).any()
			# t = matrix.todense()
			# assert (t == t.T).all()
	bulk_list = [calc_bulk(slicing(matrix_list, slc)) for slc in data_list]
	library_size_list = [bulk.sum() for bulk in bulk_list]
	print(f'library sizes =', ' '.join(map('{:.2e}'.format, library_size_list)))


def downsample_clip(matrix_list, count, mode='global'):
	assert mode == 'global'
	for m in matrix_list:
		c = m.data.sum()
		if c <= count: continue
		r = count / c
		mask_u = m.row > m.col
		mask_l = m.row < m.col
		assert mask_u.sum() == mask_l.sum()
		mask = ~mask_l
		m.data[mask] = np.random.binomial(m.data[mask].astype(int), r).astype(np.float32)
		m.col[mask_l] = m.row[mask_u]
		m.row[mask_l] = m.col[mask_u]
		m.data[mask_l] = m.data[mask_u]
		m.eliminate_zeros()
		assert not np.isnan(m.data).any()


def filter_bin(matrix_list=None, bulk=None, is_sym=True):
	if bulk is None: bulk = calc_bulk(matrix_list)

	def get_mapping(c, l):
		v = c > min(0., 0.01 * l)
		m = np.cumsum(v) - 1
		m[~v] = -1
		n = v.sum()
		print(f'{n} out of {len(c)} bins are valid')
		return m, n
	bin_id_mapping_row, num_bins_row = get_mapping(bulk.sum(1), bulk.shape[1])
	if is_sym:
		bin_id_mapping_col, num_bins_col = bin_id_mapping_row, num_bins_row
	else:
		bin_id_mapping_col, num_bins_col = get_mapping(bulk.sum(0), bulk.shape[0])
	return bin_id_mapping_row, num_bins_row, bin_id_mapping_col, num_bins_col


def slice_rearrange(matrix, size, fac_size):
	new_m = []
	patch_size = min(2 * size + 1, matrix.shape[1])
	if matrix.shape[-1] <= patch_size:
		return matrix
	for i in range(matrix.shape[0]):
		temp = matrix[i, max(int(i / fac_size) - size, 0):min(int(i / fac_size) + size + 1, matrix.shape[0])]
		if len(temp) == 0:
			print(i - size, i + size + 1, matrix.shape)
			raise EOFError
		if len(temp) < patch_size:
			temp = np.concatenate([temp, np.zeros(patch_size - len(temp))])
		new_m.append(temp)
	matrix = np.stack(new_m)
	return matrix


def kth_diag_indices(a, k):
	rows, cols = np.diag_indices_from(a)
	if k < 0:
		return rows[-k:], cols[:k]
	elif k > 0:
		return rows[:-k], cols[k:]
	else:
		return rows, cols


def get_expected(matrix):
	expected = []
	for k in range(len(matrix)):
		diag = np.diag(matrix, k)
		expected.append(np.mean(diag))
	return np.array(expected)


def oe(matrix, expected=None):
	new_matrix = np.zeros_like(matrix)
	for k in range(len(matrix)):
		rows, cols = kth_diag_indices(matrix, k)
		diag = np.diag(matrix, k)
		if expected is not None:
			expect = expected[k]
		else:
			expect = np.mean(diag)
		if expect == 0:
			new_matrix[rows, cols] = 0.0
		else:
			new_matrix[rows, cols] = diag / (expect)
	new_matrix = new_matrix + new_matrix.T - np.diag(np.diagonal(new_matrix))
	return new_matrix
