import torch
import torch.nn as nn

import torch.backends.cudnn as cudnn

from model import str2model, load_model

from data import str2dataset

from utils import test, get_wasserstein_attack_parser, set_seed, tensor_norm
from utils import float_or_none
from utils import check_hypercube

from wasserstein_attack import WassersteinAttack
from projection import dual_projection

from projection import dual_capacity_constrained_projection


class ProjectedGradient(WassersteinAttack):

    def __init__(self,
                 predict, loss_fn,
                 eps, kernel_size,
                 lr, nb_iter,
                 dual_max_iter,
                 grad_tol, int_tol,
                 device="cuda",
                 clip_min=0., clip_max=1.,
                 clipping=False,
                 postprocess=False,
                 verbose=True,
                 capacity_proj_mod=-1,
                 ):
        super().__init__(predict, loss_fn,
                         eps, kernel_size,
                         nb_iter,
                         device,
                         clip_min, clip_max,
                         clipping=clipping,
                         postprocess=postprocess,
                         verbose=verbose,
                         )
        self.lr = lr
        self.dual_max_iter = dual_max_iter

        self.grad_tol = grad_tol
        self.int_tol = int_tol

        self.postprocess = postprocess
        self.capacity_proj_mod = capacity_proj_mod

        self.inf = 1000000

    def perturb(self, X, y):
        batch_size, c, h, w = X.size()

        self.initialize_cost(X, inf=self.inf)
        pi = self.initialize_coupling(X).clone().detach().requires_grad_(True)
        normalization = X.sum(dim=(1, 2, 3), keepdim=True)

        for t in range(self.nb_iter):
            adv_example = self.coupling2adversarial(pi, X)
            scores = self.predict(adv_example.clamp(min=self.clip_min, max=self.clip_max))

            loss = self.loss_fn(scores, y)
            loss.backward()

            with torch.no_grad():
                self.lst_loss.append(loss.item())
                self.lst_acc.append((scores.max(dim=1)[1] == y).sum().item())

                """
                # pi.grad /= tensor_norm(pi.grad, p='inf').view(-1, 1, 1, 1)
                add a small constant to enhance numerical stability
                """
                pi.grad /= (tensor_norm(pi.grad, p='inf').view(batch_size, 1, 1, 1) + 1e-8)
                pi += self.lr * pi.grad

                start = torch.cuda.Event(enable_timing=True)
                end = torch.cuda.Event(enable_timing=True)

                start.record()

                if self.capacity_proj_mod == -1:
                    pi, num_iter = dual_projection(pi,
                                                   X,
                                                   cost=self.cost,
                                                   eps=self.eps * normalization.squeeze(),
                                                   dual_max_iter=self.dual_max_iter,
                                                   grad_tol=self.grad_tol,
                                                   int_tol=self.int_tol)

                elif (t + 1) % self.capacity_proj_mod == 0:
                    pi = dual_capacity_constrained_projection(pi,
                                                              X,
                                                              self.cost,
                                                              eps=self.eps * normalization.squeeze(),
                                                              transpose_idx=self.forward_idx,
                                                              detranspose_idx=self.backward_idx,
                                                              coupling2adversarial=self.coupling2adversarial)
                    num_iter = 3000

                end.record()

                torch.cuda.synchronize()

                self.run_time += start.elapsed_time(end)
                self.num_iter += num_iter
                self.func_calls += 1

                if self.verbose and (t + 1) % 10 == 0:
                    print("num of iters : {:4d}, ".format(t + 1),
                          "loss : {:9.3f}, ".format(loss.item()),
                          "acc : {:5.2f}%, ".format((scores.max(dim=1)[1] == y).sum().item() / batch_size * 100),
                          "dual iter : {:4d}, ".format(num_iter),
                          "per iter time : {:7.3f}ms".format(start.elapsed_time(end) / num_iter))

                self.check_nonnegativity(pi / normalization, tol=1e-6, verbose=False)
                self.check_marginal_constraint(pi / normalization, X / normalization, tol=1e-6, verbose=False)
                self.check_transport_cost(pi / normalization, tol=1e-3, verbose=False)

            pi = pi.clone().detach().requires_grad_(True)

        with torch.no_grad():
            adv_example = self.coupling2adversarial(pi, X)
            check_hypercube(adv_example, verbose=self.verbose)
            self.check_nonnegativity(pi / normalization, tol=1e-5, verbose=self.verbose)
            self.check_marginal_constraint(pi / normalization, X / normalization, tol=1e-5, verbose=self.verbose)
            self.check_transport_cost(pi / normalization, tol=self.eps * 1e-3, verbose=self.verbose)

            if self.postprocess is True:
                if self.verbose:
                    print("==========> post-processing projection........")

                pi = dual_capacity_constrained_projection(pi,
                                                          X,
                                                          self.cost,
                                                          eps=self.eps * normalization.squeeze(),
                                                          transpose_idx=self.forward_idx,
                                                          detranspose_idx=self.backward_idx,
                                                          coupling2adversarial=self.coupling2adversarial)

                adv_example = self.coupling2adversarial(pi, X)
                check_hypercube(adv_example, tol=self.eps * 5e-2, verbose=self.verbose)

                self.check_nonnegativity(pi / normalization, tol=1e-6, verbose=self.verbose)
                self.check_marginal_constraint(pi / normalization, X / normalization, tol=1e-6, verbose=self.verbose)
                self.check_transport_cost(pi / normalization, tol=self.eps * 1e-3, verbose=self.verbose)

        if self.clipping:
            return adv_example.clamp(min=self.clip_min, max=self.clip_max)
        else:
            return adv_example


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(parents=[get_wasserstein_attack_parser()])

    parser.add_argument('--lr', type=float, default=0.1, help='gradient step size')
    parser.add_argument('--dual_max_iter', type=int, default=15)
    parser.add_argument('--grad_tol', type=float_or_none, default=1e-4)
    parser.add_argument('--int_tol', type=float_or_none, default=1e-4)

    parser.add_argument('--capacity_proj_mod', type=int, default=-1)

    args = parser.parse_args()

    print(args)

    device = "cuda"

    set_seed(args.seed)

    if args.benchmark:
        cudnn.benchmark = True

    testset, normalize, unnormalize = str2dataset(args.dataset)
    testloader = torch.utils.data.DataLoader(testset, batch_size=args.batch_size, shuffle=False, num_workers=0)

    net = str2model(args.checkpoint, dataset=args.dataset, pretrained=True).eval().to(device)

    for param in net.parameters():
        param.requires_grad = False

    projected_gradient = ProjectedGradient(predict=lambda x: net(normalize(x)),
                                           loss_fn=nn.CrossEntropyLoss(reduction="sum"),
                                           eps=args.eps,
                                           kernel_size=args.kernel_size,
                                           lr=args.lr,
                                           nb_iter=args.nb_iter,
                                           dual_max_iter=args.dual_max_iter,
                                           grad_tol=args.grad_tol,
                                           int_tol=args.int_tol,
                                           device=device,
                                           clip_min=0.0, clip_max=1.0,
                                           clipping=False,
                                           postprocess=args.postprocess,
                                           verbose=True,
                                           capacity_proj_mod=args.capacity_proj_mod)

    acc = test(lambda x: net(normalize(x)),
               testloader,
               device=device,
               attacker=projected_gradient,
               num_batch=args.num_batch,
               save_img_loc=args.save_img_loc)

    projected_gradient.print_info(acc)

    if args.save_info_loc is not None:
        projected_gradient.save_info(acc, args.save_info_loc)
