#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import logging
import numpy as np
from functools import partial
import matplotlib.pyplot as pl
from multiprocessing import Pool
from scipy.optimize import minimize

import george
from george import kernels
from george.modeling import ModelingMixin

import transit

from peerless._search import search
from peerless.catalogs import KICatalog
from peerless.data import (load_light_curves_for_kic, running_median_trend,
                           load_light_curves)


def get_peaks(kicid=None,
              lcs=None,
              tau=0.6,
              detrend_hw=2.0,
              remove_kois=True,
              grid_frac=0.25,
              noise_hw=15.0,
              detect_thresh=20.0,
              max_peaks=3,
              output_dir="output",
              plot_all=False,
              delete=True,
              verbose=False):
    """
    :param tau:
        The transit duration. (default: 0.6)

    :param detrend_hw:
        Half width of running window for de-trending. (default: 2.0)

    :param remove_kois:
        Remove data points near known KOI transits. (default: True)

    :param grid_frac:
        The search grid spacing as a fraction of the duration. (default: 0.25)

    :param noise_hw:
        Half width of running window for noise estimation. (default: 15.0)

    :param detect_thresh:
        Relative S/N detection threshold. (default: 20.0)

    :param max_peaks:
        The maximum number of peaks to analyze in detail. (default: 3)

    :param output_dir:
        The parent directory for the plots. (default: output)

    :param plot_all:
        Make all the plots instead of just the transit plots. (default: False)

    :param delete:
        Delete the light curve files after loading them. (default: True)

    :param verbose:
        Moar printing. (default: False)

    """
    if lcs is None and kicid is None:
        raise ValueError("you must specify 'lcs' or 'kicid'")
    if lcs is None:
        lcs = load_light_curves_for_kic(kicid, delete=delete,
                                        remove_kois=remove_kois)
    else:
        kicid = "unknown-target"

    # Loop over light curves and search each one.
    time = []
    chunk = []
    depth = []
    depth_ivar = []
    s2n = []
    for i, lc in enumerate(lcs):
        time.append(np.arange(lc.time.min(), lc.time.max(), grid_frac * tau))
        d, ivar, s = search(tau, time[-1], lc.time, lc.flux-1, 1/lc.ferr**2)
        depth.append(d)
        depth_ivar.append(ivar)
        s2n.append(s)
        chunk.append(i + np.zeros(len(time[-1]), dtype=int))
    time = np.concatenate(time)
    chunk = np.concatenate(chunk)
    depth = np.concatenate(depth)
    depth_ivar = np.concatenate(depth_ivar)
    s2n = np.concatenate(s2n)

    # Compute the depth S/N time series and smooth it to estimate a background
    # noise level.
    m = depth_ivar > 0.0
    noise = np.nan + np.zeros_like(s2n)
    noise[m] = running_median_trend(time[m], np.abs(s2n[m]), noise_hw)

    # Find peaks about the fiducial threshold.
    m = s2n > detect_thresh * noise
    peaks = []
    while np.any(m):
        i = np.argmax(s2n[m])
        t0 = time[m][i]
        peaks.append(dict(
            kicid=kicid,
            t0=t0 + 0.5 * tau,
            s2n=s2n[m][i],
            bkg=noise[m][i],
            depth=depth[m][i],
            depth_ivar=depth_ivar[m][i],
            chunk=chunk[m][i],
        ))
        m &= np.abs(time - t0) > 2*tau

    if verbose:
        print("Found {0} raw peaks".format(len(peaks)))

    if not len(peaks):
        return []

    if len(peaks) > max_peaks:
        logging.warning("truncating peak list")
    peaks = peaks[:max_peaks]

    # For each peak, plot the diagnostic plots and vet.
    basedir = os.path.join(output_dir, "{0}".format(kicid))
    for i, peak in enumerate(peaks):
        # Vetting.
        t0 = peak["t0"]
        d = peak["depth"]
        chunk = peak["chunk"]
        lc0 = lcs[chunk]
        x = lc0.raw_time
        y = lc0.raw_flux
        yerr = lc0.raw_ferr

        # Mean models:
        # 1. constant
        constant = np.mean(y)

        # 2. transit
        system = transit.SimpleSystem(
            period=3000.0,
            t0=t0,
            ror=np.sqrt(d),
            duration=tau,
            impact=0.5,
        )
        system.freeze_parameter("ln_period")

        # Loop over models and compare them.
        preds = []
        for name, mean_model in [("gp", constant),
                                 ("transit", system)]:
            kernel = np.var(y) * kernels.Matern32Kernel(2**2)
            gp = george.GP(kernel, mean=mean_model, fit_mean=True,
                           white_noise=2*np.log(np.mean(yerr)),
                           fit_white_noise=True)
            gp.compute(x, yerr)

            bounds = gp.get_bounds()
            n = gp.get_parameter_names()
            if "mean:t0" in n:
                bounds[n.index("mean:t0")] = (t0 - 0.5*tau, t0 + 0.5*tau)
            if "mean:impact" in n:
                bounds[n.index("mean:impact")] = (0, 1.0)
            if "kernel:k2:ln_M_0_0" in n:
                bounds[n.index("kernel:k2:ln_M_0_0")] = (
                    np.log(0.1), None
                )

            # Optimize.
            r = minimize(gp.nll, gp.get_vector(), jac=gp.grad_nll, args=(y,),
                         method="L-BFGS-B", bounds=bounds)
            if verbose:
                print(r)
            if not r.success:
                peak["lnlike_{0}".format(name)] = -r.fun
                peak["bic_{0}".format(name)] = -np.inf
                continue

            gp.set_vector(r.x)
            preds.append(gp.predict(y, x, return_cov=False))

            # Compute the -0.5*BIC.
            peak["lnlike_{0}".format(name)] = -r.fun
            peak["bic_{0}".format(name)] = -r.fun - 0.5*len(r.x)*np.log(len(x))

            # Save the transit parameters.
            peak["transit_duration"] = system.duration
            peak["transit_ror"] = system.ror
            peak["transit_time"] = system.t0
            peak["transit_impact"] = system.impact

        if peak["bic_transit"] < peak["bic_gp"] and not plot_all:
            continue

        # Plots.
        fig, axes = pl.subplots(3, 2, figsize=(10, 8))

        # Raw flux.
        row = axes[0]
        for ax in row:
            [ax.plot(lc.raw_time, (lc.raw_flux-1)*1e3, "-k") for lc in lcs]

            ax.set_xticklabels([])
            ax.yaxis.set_major_locator(pl.MaxNLocator(4))
            ax.xaxis.set_major_locator(pl.MaxNLocator(5))

        row[0].set_ylabel("raw [ppt]")
        ax = row[1]
        [ax.plot(x, (p-1)*1e3) for p in preds]
        ax.plot(x, (system.get_value(x)-1)*1e3)

        # De-trended flux.
        row = axes[1]
        for ax in row:
            [ax.plot(lc.time, (lc.flux-1)*1e3, "-k") for lc in lcs]
            ax.set_xticklabels([])
            ax.yaxis.set_major_locator(pl.MaxNLocator(4))
            ax.xaxis.set_major_locator(pl.MaxNLocator(5))
        row[0].set_ylabel("de-trended [ppt]")

        # Periodogram.
        row = axes[2]
        for ax in row:
            ax.plot(time + 0.5*tau, s2n, "k")
            ax.plot(time + 0.5*tau, noise, "g")
            ax.plot(time + 0.5*tau, detect_thresh * noise, ":g")
            ax.yaxis.set_major_locator(pl.MaxNLocator(4))
            ax.xaxis.set_major_locator(pl.MaxNLocator(5))
            ax.set_xlabel("time [KBJD]")
        row[0].set_ylabel("s/n")

        for ax1, ax2 in axes:
            ax1.yaxis.set_label_coords(-0.1, 0.5)

            ax1.set_xlim(time.min() - 5.0, time.max() + 5.0)
            ax1.axvline(t0, color="g", lw=5, alpha=0.3)

            ax2.set_xlim(t0 - 5.0, t0 + 5.0)
            ax2.axvline(t0, color="g", lw=5, alpha=0.3)
            ax2.axvline(t0 - 0.5*tau, color="k", ls="dashed")
            ax2.axvline(t0 + 0.5*tau, color="k", ls="dashed")
            ax2.set_yticklabels([])

        fig.subplots_adjust(
            left=0.1, bottom=0.1, right=0.98, top=0.97,
            wspace=0.05, hspace=0.12
        )
        os.makedirs(basedir, exist_ok=True)
        fig.savefig(os.path.join(basedir, "{0:04d}.png".format(i + 1)))
        pl.close(fig)

    return peaks


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="search for single transits")

    parser.add_argument("kicids", nargs="*", help="some KIC IDs")
    parser.add_argument("--max-targets", type=int,
                        help="the maximum number of targets")
    parser.add_argument("-f", "--filenames", nargs="+",
                        help="some light curve filenames")
    parser.add_argument("-p", "--parallel", action="store_true",
                        help="parallelize across targets")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="more output to the screen")
    parser.add_argument("-c", "--clean", action="store_true",
                        help="remove temporary light curve files")
    parser.add_argument("-o", "--output-dir", default="output",
                        help="the output directory")
    parser.add_argument("--plot-all", action="store_true",
                        help="make all the plots")

    # Preset target lists:
    parser.add_argument("--planet-hunters", action="store_true",
                        help="search the planet hunter targets")
    parser.add_argument("--bright-g-dwarfs", action="store_true",
                        help="search bright G and K dwarfs")

    # Search parameters:
    parser.add_argument("--duration", type=float,  default=0.6,
                        help="the transit duration in days")
    parser.add_argument("--detrend-hw", type=float,  default=2.0,
                        help="the half width of the de-trending window")
    parser.add_argument("--no-remove-kois", action="store_true",
                        help="leave the known KOIs in the light curves")
    parser.add_argument("--grid-frac", type=float,  default=0.25,
                        help="search grid spacing in units of the duration")
    parser.add_argument("--noise-hw", type=float,  default=15.0,
                        help="the half width of the noise estimation window")
    parser.add_argument("--detect-thresh", type=float,  default=20.0,
                        help="the relative detection threshold")
    parser.add_argument("--max-peaks", type=int,  default=3,
                        help="the maximum number of peaks to consider")

    args = parser.parse_args()

    # Build the dictionary of search keywords.
    function = partial(
        get_peaks,
        tau=args.duration,
        detrend_hw=args.detrend_hw,
        remove_kois=not args.no_remove_kois,
        grid_frac=args.grid_frac,
        noise_hw=args.noise_hw,
        detect_thresh=args.detect_thresh,
        output_dir=args.output_dir,
        plot_all=args.plot_all,
        max_peaks=args.max_peaks,
        verbose=args.verbose,
        delete=args.clean,
    )

    # Build the list of KIC IDs.
    kicids = args.kicids

    # Presets.
    if args.planet_hunters:
        kicids += [
            2158850, 3558849, 5010054, 5536555, 5951458, 8410697, 8510748,
            8540376, 9704149, 9838291, 10024862, 10403228, 10842718, 10960865,
            11558724, 12066509,
        ]
    if args.bright_g_dwarfs:
        stlr = KICatalog().df
        m = (4200 <= stlr.teff) & (stlr.teff <= 6100)
        m &= stlr.radius <= 1.15
        m &= stlr.dataspan > 365.25*2.
        m &= stlr.dutycycle > 0.6
        m &= stlr.rrmscdpp07p5 <= 1000.
        m &= stlr.kepmag < 15.
        kicids += list(np.array(stlr[m].kepid))
    kicids = np.array(kicids, dtype=int)

    # Limit the target list.
    if args.max_targets is not None:
        if len(kicids) > args.max_targets:
            logging.warning("Truncating target list from {0} to {1}".format(
                len(kicids), args.max_targets
            ))
        kicids = kicids[:args.max_targets]

    # Check and create the output directory.
    if os.path.exists(args.output_dir):
        logging.warning("Output directory '{0}' exists"
                        .format(args.output_dir))
    else:
        os.makedirs(args.output_dir)
    cand_fn = os.path.join(args.output_dir, "candidates.csv")
    columns = [
        "kicid", "chunk", "t0", "s2n", "bkg", "depth", "depth_ivar",
        "lnlike_gp", "lnlike_transit", "bic_gp", "bic_transit",
        "transit_time", "transit_ror", "transit_duration", "transit_impact",
    ]
    with open(cand_fn, "w") as f:
        f.write("# {0}\n".format(", ".join(columns)))
    with open(os.path.join(args.output_dir, "targets.csv"), "w") as f:
        f.write("\n".join(map("{0}".format, kicids)))

    if len(kicids):
        # Deal with parallelization.
        if args.parallel:
            pool = Pool()
            M = pool.imap_unordered
        else:
            M = map

        for i, peaks in enumerate(M(function, kicids)):
            if (i + 1) % 500 == 0:
                sys.stderr.write('\rdone {0:%}'.format((i + 1)/len(kicids)))
            if not len(peaks):
                continue
            with open(cand_fn, "a") as f:
                f.write("\n".join(
                    ", ".join("{0}".format(p[k]) for k in columns)
                    for p in peaks) + "\n")

    if args.filenames is not None:
        lcs = load_light_curves(
            args.filenames,
            detrend_hw=args.detrend_hw,
            remove_kois=not args.no_remove_kois,
        )
        peaks = function(lcs=lcs)
        if len(peaks):
            with open(cand_fn, "a") as f:
                f.write("\n".join(
                    ", ".join("{0}".format(p[k]) for k in columns)
                    for p in peaks) + "\n")
