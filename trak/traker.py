from typing import Iterable, Optional
import torch as ch
from torch.nn.parameter import Parameter
from torch import Tensor
from trak.modelout_functions import AbstractModelOutput
from trak.projectors import BasicProjector, ProjectionType
from trak.utils import parameters_to_vector, vectorize_and_ignore_buffers
try:
    from functorch import make_functional_with_buffers, grad, vmap
except ImportError:
    print('Cannot import `functorch`. Functional mode cannot be used.')

class TRAKer():
    def __init__(self,
                 model,
                 model_output_fn: AbstractModelOutput,
                 proj_dim=10,
                 projector=BasicProjector,
                 proj_type=ProjectionType.normal,
                 proj_seed=0,
                 save_dir: str='./trak_results',
                 device=None,
                 train_set_size=1,
                 grad_dtype=ch.float16,
                 load_from: Optional[str]=None):
        """ Main class for computing TRAK scores.
        See [User guide link here] for detailed examples.

        Parameters
        ----------
        save_dir : str, default='./trak_results'
            Directory to save TRAK scores and intermediate values
            like projected gradients of the train set samples and
            targets.

        Attributes
        ----------

        """
        self.save_dir = save_dir
        self.model = model
        self.model_output_fn = model_output_fn
        self.device = device
        self.grad_dtype = grad_dtype

        self.last_ind = 0

        self.projector = projector(seed=proj_seed,
                                   proj_dim=proj_dim,
                                   grad_dim=parameters_to_vector(self.model.parameters()).numel(),
                                   proj_type=proj_type,
                                   dtype=self.grad_dtype,
                                   device=self.device)
        
        self.train_set_size = train_set_size

        self.model_params = parameters_to_vector(model.parameters())
        self.grad_dim = self.model_params.numel()


        self.grads = ch.zeros([train_set_size, proj_dim])
    
    def featurize(self,
                  out_fn,
                  loss_fn,
                  model,
                  batch: Iterable[Tensor],
                  inds: Optional[Iterable[int]]=None,
                  functional: bool=False) -> Tensor:
        if functional:
            func_model, weights, buffers = model
            self._featurize_functional(out_fn, weights, buffers, batch, inds)
            loss_grads = self._get_loss_grad_functional(func_model,
                                                        weights,
                                                        buffers,
                                                        batch,
                                                        inds)
        else:
            self._featurize_iter(out_fn, model, batch, inds)
            self._get_loss_grad_iter(model, batch, inds)

    
    def _featurize_functional(self, out_fn, weights, buffers, batch, inds) -> Tensor:
        """
        Using the `vmap` feature of `functorch`
        """

        grads_loss = grad(out_fn, has_aux=False)
        # map over batch dimension
        grads = vmap(grads_loss,
                     in_dims=(None, None, *([0] * len(batch))),
                     randomness='different')(weights, buffers, *batch)
        self.record_grads(vectorize_and_ignore_buffers(grads), inds)

    def _featurize_iter(self, out_fn, model, batch, batch_size=None) -> Tensor:
        """Computes per-sample gradients of the model output function
        This method does not leverage vectorization (and is hence much slower than
        `_featurize_vmap`).
        """
        if batch_size is None:
            # assuming batch is an iterable of torch Tensors, each of
            # shape [batch_size, ...]
            batch_size = batch[0].size(0)

        grads = ch.zeros(batch_size, self.grad_dim).cuda()
        margin = out_fn(model, batch)

        for ind in range(batch_size):
            grads[ind] = parameters_to_vector(ch.autograd.grad(margin[ind],
                                                               model.parameters(),
                                                               retain_graph=True))

        return grads

    def record_grads(self, grads, inds):
        grads = self.projector.project(grads.to(self.grad_dtype))
        self.grads[inds] = grads.detach().clone().cpu()
    
    def _get_loss_grad_functional(self, func_model, weights, buffers, batch, inds):
        """Computes
        .. math::
            \partial \ell / \partial \text{margin}
        
        """
        images, labels = batch  # hardcoded for CV for now
        logits = func_model(weights, buffers, images)
        return self.model_output_fn.get_out_to_loss(logits, labels)


    def finalize(self, out_dir: Optional[str] = None, 
                       cleanup: bool = False, 
                       agg: bool = False):
        pass

    def score(self, val: Tensor, params: Iterable[Parameter]) -> Tensor:
        return ch.zeros(1, 1)