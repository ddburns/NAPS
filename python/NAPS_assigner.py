#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Mon Nov 19 10:36:07 2018

@author: aph516
"""

import numpy as np
import pandas as pd
from plotnine import *
from scipy.stats import norm, multivariate_normal
from scipy.optimize import linear_sum_assignment
from math import isnan, log10, sqrt
from copy import deepcopy
#from Bio.SeqUtils import seq1
from distutils.util import strtobool
import logging

class NAPS_assigner:
    # Functions
    def __init__(self):
        self.obs = None
        self.preds = None
        self.log_prob_matrix = None
        self.assign_df = None
        self.alt_assign_df = None
        self.best_match_indexes = None
        self.pars = {"pred_offset": 0,
                "prob_method": "pdf",
                "pred_correction": False,
                "delta_correlation": False,
                "alt_assignments": 1,
                "atom_set": {"H","N","HA","C","CA","CB","Cm1","CAm1","CBm1"},
                "atom_sd": {'H':0.1711, 'N':1.1169, 'HA':0.1231,
                            'C':0.5330, 'CA':0.4412, 'CB':0.5163,
                            'Cm1':0.5530, 'CAm1':0.4412, 'CBm1':0.5163},
                "plot_strips": False}
            
    def read_config_file(self, filename):
        config = pd.read_table(filename, sep="\s+", comment="#", header=None,
                               index_col=0, names=["Value"]).to_dict()["Value"]
        
        self.pars["pred_offset"] = int(config["pred_offset"])
        self.pars["prob_method"] = config["prob_method"]
        self.pars["pred_correction"] = bool(strtobool(config["pred_correction"]))
        self.pars["delta_correlation"] = bool(strtobool(config["delta_correlation"]))
        self.pars["alt_assignments"] = int(config["alt_assignments"])
        self.pars["atom_set"] = {s.strip() for s in config["atom_set"].split(",")}
        tmp = [s.strip() for s in config["atom_sd"].split(",")]
        self.pars["atom_sd"] = dict([(x.split(":")[0], float(x.split(":")[1])) for x in tmp])
        self.pars["plot_strips"] = bool(strtobool(config["plot_strips"]))
        return(self.pars)
    
    def import_pred_shifts(self, input_file, filetype, offset=None):
        """ Import predicted chemical shifts from a ShiftX2 results file.
        
        filetype: either "shiftx2" or "sparta+"
        offset: an optional integer to add to the ShiftX2 residue number.
        """
        
        # If no offset value is defined, use the default one
        if offset==None:
            offset = self.pars["pred_offset"]
        
        if filetype == "shiftx2":
            preds_long = pd.read_csv(input_file)
            if any(preds_long.columns == "CHAIN"):
                if len(preds_long["CHAIN"].unique())>1:
                    print("Chain identifier dropped - if multiple chains are "+
                          "present in the predictions, they will be merged.")
                preds_long = preds_long.drop("CHAIN", axis=1)     
            preds_long = preds_long.reindex(columns=["NUM","RES","ATOMNAME",
                                                     "SHIFT"])  
            preds_long.columns = ["Res_N","Res_type","Atom_type","Shift"]
        elif filetype == "sparta+":
            # Work out where the column names and data are
            with open(input_file, 'r') as f:
                for num, line in enumerate(f, 1):
                    if line.find("VARS")>-1:
                        colnames_line = num
                        colnames = line.split()[1:]
                        break
                        
            preds_long = pd.read_table(input_file, sep="\s+", names=colnames,
                                       skiprows=colnames_line+1)
            preds_long = preds_long[["RESID","RESNAME","ATOMNAME","SHIFT"]]
            preds_long.columns = ["Res_N","Res_type","Atom_type","Shift"]
            
            # Sparta+ uses HN for backbone amide proton - convert to H
            preds_long.loc[preds_long["Atom_type"]=="HN", "Atom_type"] = "H"
        else:
            print("import_pred_shifts: invalid filetype '%s'." % (filetype))
            return(None)
        
        # Add sequence number offset and create residue names
        preds_long["Res_N"] = preds_long["Res_N"] + offset
        preds_long.insert(1, "Res_name", (preds_long["Res_N"].astype(str) + 
                  preds_long["Res_type"]))
        preds_long["Res_name"] = [s.rjust(5) for s in preds_long["Res_name"]]
            
        # Convert from long to wide format
        preds = preds_long.pivot(index="Res_N", columns="Atom_type", 
                                 values="Shift")
        
        # Add the other data back in
        tmp = preds_long[["Res_N","Res_type","Res_name"]]
        tmp = tmp.drop_duplicates(subset="Res_name")
        tmp.index = tmp["Res_N"]
        preds = pd.concat([tmp, preds], axis=1)
        
        # Make columns for the i-1 predicted shifts of C, CA and CB
        preds_m1 = preds[list({"C","CA","CB","Res_type"}.
                              intersection(preds.columns))].copy()
        preds_m1.index = preds_m1.index+1
        preds_m1.columns = preds_m1.columns + "m1"
        preds = pd.merge(preds, preds_m1, how="left", 
                         left_index=True, right_index=True)
        
        # Restrict to only certain atom types
        atom_set = {"H","N","C","CA","CB","Cm1","CAm1","CBm1","HA"}
        preds = preds[["Res_name","Res_N","Res_type","Res_typem1"]+
                      list(atom_set.intersection(preds.columns))]
        
        preds.index = preds["Res_name"]
        preds.index.name = None
        
        self.preds = preds
        return(self.preds)
    
    
    def add_dummy_rows(self):
        """Add dummy rows to obs and preds to bring them to the same length.
        
        Also discard any atom types that aren't present in both obs and preds.
        """
        
        obs = self.obs.copy()
        preds = self.preds.copy()
        
        # Delete any prolines in preds
        preds = preds.drop(preds.index[preds["Res_type"]=="P"])
        
        # Restrict atom types
        # self.pars["atom_set"] is the set of atoms to be used in the analysis
        obs_metadata = list(set(obs.columns).difference(self.pars["atom_set"]))     
        preds_metadata = list(set(preds.columns).
                              difference(self.pars["atom_set"]))
        shared_atoms = list(self.pars["atom_set"].intersection(obs.columns).
                            intersection(preds.columns))
        obs = obs.loc[:,obs_metadata+shared_atoms]
        preds = preds.loc[:,preds_metadata+shared_atoms]
        
        # Create columns to keep track of dummy status
        preds["Dummy_res"] = False
        obs["Dummy_SS"] = False

        N = len(obs.index)
        M = len(preds.index)
        
        if N>M:     # If there are more spin systems than predictions
            dummies = pd.DataFrame(np.NaN, columns = preds.columns, 
                        index=["dummy_res_"+str(i) for i in 1+np.arange(N-M)])
            dummies["Res_name"] = dummies.index
            dummies["Dummy_res"] = True
            preds = preds.append(dummies)        
        elif M>N:
            dummies = pd.DataFrame(np.NaN, columns = obs.columns, 
                        index=["dummy_SS_"+str(i) for i in 1+np.arange(M-N)])
            dummies["SS_name"] = dummies.index
            dummies["Dummy_SS"] = True
            obs = obs.append(dummies)
            #obs.loc[["dummy_"+str(i) for i in 1+np.arange(M-N)]] = np.NaN
            #obs.loc[obs.index[N:M], "SS_name"] = ["dummy_"+str(i) for i in 1+np.arange(M-N)]

        self.obs = obs.copy()
        self.preds = preds.copy()
        
        return(self.obs, self.preds)
    
    def calc_log_prob_matrix(self, atom_sd=None, sf=1, default_prob=0.01, 
                             use_hadamac=False, cdf=False, rescale_delta=False, 
                             delta_correlation=False, shift_correlation=False,
                             verbose=False):
        """Calculate a matrix of -log10(match probabilities)
        
        use_hadamac: if True, amino acid type information will contribute to 
            the log probability
        cdf: if True, use cdf in probability matrix. Otherwise use pdf (cdf 
            uses chance of seeing a delta 'at least this great')
        rescale_delta: if True, the shift differences between obs and pred are 
            scaled so they are closer to the normal distribution
        delta_correlation: if True, correlated errors between different atom 
            types are accounted for in the probability 
        shift_correlation: if True, the correlation between observed shift and
            prediction error is accounted for.
        """
        
        # Use default atom_sd values if not defined
        if atom_sd==None:
            atom_sd = self.pars["atom_sd"]
#            atom_sd={'H':0.1711, 'N':1.1169, 'HA':0.1231,
#                     'C':0.5330, 'CA':0.4412, 'CB':0.5163,
#                     'Cm1':0.5530, 'CAm1':0.4412, 'CBm1':0.5163}
        
        def calc_match_probability(obs, pred1):
            """ Calculate match scores between all observed spin systems and a 
            single predicted residue
            
            default_prob: probability assigned when an observation or 
                prediction is missing
            atom_sd: expected standard deviation for each atom type
            sf: scaling factor for the entire atom_sd dictionary
            use_hadamac: determines whether residue type information is used
            """
            
            # Throw away any non-atom columns
            obs_reduced = obs.loc[:, self.pars["atom_set"].
                                  intersection(obs.columns)]
            pred1_reduced = pred1.loc[self.pars["atom_set"].
                                      intersection(pred1.index)]
            
            # Calculate shift differences for each observed spin system
            delta = obs_reduced - pred1_reduced
            
            # Make a note of NA positions in delta, and set them to zero 
            # (this avoids warnings when using norm.cdf later)
            na_mask = delta.isna()
            delta[na_mask] = 0
            
            if self.pars["prob_method"] == "delta_correlation":
                overall_prob = pd.Series(index=delta.index)
                overall_prob[:] = 1
                
                d_mean = pd.read_csv("../data/d_mean.csv", header=None, 
                                     index_col=0).loc[delta.columns,1]
                d_cov = (pd.read_csv("../data/d_cov.csv", index_col=0).
                         loc[delta.columns,delta.columns])
                
                mvn = multivariate_normal(d_mean, d_cov)
                
                overall_prob = mvn.logpdf(delta)
                
                # Penalise missing shifts, unless also missing in predictions
                overall_prob = (overall_prob + log10(default_prob) * 
                            (na_mask.sum(axis=1) - pred1_reduced.isna().sum()))
                    
            else:
                prob = delta.copy()
                prob.iloc[:,:] = 1
                
                for c in delta.columns:
                    if self.pars["prob_method"] == "cdf":
                        # Use the cdf to calculate the probability of a 
                        # delta *at least* as great as the actual one
                        prob[c] = log10(2) + norm.logcdf(-1*abs(
                                pd.to_numeric(delta[c])), scale=atom_sd[c]*sf)
                    elif self.pars["prob_method"] == "pdf":
                        prob[c] = norm.logpdf(pd.to_numeric(delta[c]), 
                            scale=atom_sd[c]*sf)       
                    elif shift_correlation:
                        print("shift_correlation not yet implemented. Defaulting to pdf.")
                        prob[c] = norm.logpdf(pd.to_numeric(delta[c]), 
                            scale=atom_sd[c]*sf)
                    else:
                        print("Method for calculating probability not recognised. Defaulting to pdf.")
                        prob[c] = norm.logpdf(pd.to_numeric(delta[c]), 
                            scale=atom_sd[c]*sf)
                
                # In positions where data was missing, use default probability
                prob[na_mask] = log10(default_prob)
                
                # Calculate penalty for a HADAMAC mismatch
                if use_hadamac:
                    # If the i-1 aa type of the predicted residue matches the 
                    # HADAMAC group of the observation, probability is 1.
                    # Otherwise, probability defaults to 0.01
                    prob["SS_classm1"] = 0.01
                    if type(pred1["Res_typem1"])==str:    # dummies have NaN
                        prob.loc[obs["SS_classm1"].str.find(
                                pred1["Res_typem1"])>=0, "SS_classm1"] = 1
            
                # Calculate overall probability of each row
                overall_prob = prob.sum(skipna=False, axis=1)
                
            return(overall_prob)
        
        obs = self.obs
        preds = self.preds
        
        # Initialise matrix as NaN
        log_prob_matrix = pd.DataFrame(np.NaN, index=obs.index, 
                                       columns=preds.index)    
        
        for i in preds.index:
            if verbose: print(i)
            log_prob_matrix.loc[:, i] = calc_match_probability(obs, 
                                                               preds.loc[i,:])
        
        
        # Calculate log of matrix
        log_prob_matrix[log_prob_matrix.isna()] = 2*np.nanmin(
                                                        log_prob_matrix.values)
        log_prob_matrix.loc[obs["Dummy_SS"], :] = 0
        log_prob_matrix.loc[:, preds["Dummy_res"]] = 0
        
        self.log_prob_matrix = log_prob_matrix
        return(self.log_prob_matrix)
        
    def calc_log_prob_matrix2(self, atom_sd=None, sf=1, default_prob=0.01, 
                             use_hadamac=False, cdf=False, 
                             delta_correlation=False, shift_correlation=False,
                             verbose=False):
        """Calculate a matrix of -log10(match probabilities)
        
        use_hadamac: if True, amino acid type information will contribute to 
            the log probability
        cdf: if True, use cdf in probability matrix. Otherwise use pdf (cdf 
            uses chance of seeing a delta 'at least this great')
        delta_correlation: if True, correlated errors between different atom 
            types are accounted for in the probability 
        shift_correlation: if True, the correlation between observed shift and
            prediction error is accounted for.
        """
        
        # Use default atom_sd values if not defined
        if atom_sd==None:
            atom_sd = self.pars["atom_sd"]
#            atom_sd={'H':0.1711, 'N':1.1169, 'HA':0.1231,
#                     'C':0.5330, 'CA':0.4412, 'CB':0.5163,
#                     'Cm1':0.5530, 'CAm1':0.4412, 'CBm1':0.5163}
        
        if self.pars["pred_correction"]:
            # This hardcoded path is bad! Need to import at an earlier stage.
            lm_pars = pd.read_csv("../config/lin_model_shiftx2.csv", index_col=0)
            self.preds_corr = {}
        
        obs = self.obs
        preds = self.preds
        atoms = self.pars["atom_set"].intersection(obs.columns)
    
        log_prob_matrix = pd.DataFrame(0, index=obs.index, columns=preds.index)
        
        for atom in atoms:
            # The most efficient way I've found to do the calculation is to 
            # take the obs and preds shift columns for an atom, repeat each 
            # into a matrix, then subtract these matrixes from each other. 
            # That way, all calculations take advantage of vectorisation. 
            # Much faster than using loops.
            obs_atom = pd.DataFrame(obs[atom].repeat(len(obs.index)).values.
                                reshape([len(obs.index),-1]),
                                index=preds.index, columns=obs.index)
            preds_atom = pd.DataFrame(preds[atom].repeat(len(preds.index)).values.
                                reshape([len(preds.index),-1]).transpose(),
                                index=preds.index, columns=obs.index)
            
            
            # If predicting corrections, apply a linear transformation of delta
            if self.pars["pred_correction"]:
                preds_corr_atom = preds_atom
                for res in preds["Res_type"].unique():
                    if (atom+"_"+res) in lm_pars.index:
                         
                        grad = lm_pars.loc[(lm_pars["Atom_type"]==atom) & 
                                           (lm_pars["Res_type"]==res),"Grad"].tolist()[0]
                        offset = lm_pars.loc[(lm_pars["Atom_type"]==atom) & 
                                           (lm_pars["Res_type"]==res),"Offset"].tolist()[0]
                        preds_corr_atom.loc[preds["Res_type"]==res,:] = (preds_atom.loc[preds["Res_type"]==res, :]
                                                            - grad * obs_atom.loc[preds["Res_type"]==res, :]
                                                            - offset) 
                delta_atom = preds_corr_atom - obs_atom
                self.preds_corr[atom] = preds_corr_atom
            else:
                delta_atom = preds_atom - obs_atom
            
            # Make a note of NA positions in delta, and set them to zero 
            # (this avoids warnings when using norm.cdf later)
            na_mask = np.isnan(delta_atom)
            delta_atom[na_mask] = 0
            
            if self.pars["prob_method"] == "cdf":
                # Use the cdf to calculate the probability of a 
                # delta *at least* as great as the actual one
                prob_atom = pd.DataFrame(-2*norm.logcdf(abs(delta_atom), scale=atom_sd[atom]),
                                     index=obs.index, columns=preds.index)
            elif self.pars["prob_method"] == "pdf":
                prob_atom = pd.DataFrame(norm.logpdf(delta_atom, scale=atom_sd[atom]),
                                     index=obs.index, columns=preds.index)
            else:
                print("Method for calculating probability not recognised. Defaulting to pdf.")
                prob_atom = pd.DataFrame(norm.logpdf(delta_atom, scale=atom_sd[atom]),
                                     index=obs.index, columns=preds.index)
            
            
            prob_atom[na_mask] = log10(default_prob)
            
            log_prob_matrix = log_prob_matrix + prob_atom
        
        if use_hadamac:
            # For each type of residue type information that's available, make a 
            # matrix showing the probability modifications due to type mismatch, 
            # then add it to log_prob_matrix
            # Maybe make SS_class mismatch a parameter in config file?
            for ss_class in {"SS_class","SS_classm1"}.intersection(obs.columns):
                print(ss_class)
                SS_class_matrix = pd.DataFrame(0, index=log_prob_matrix.index, 
                                           columns=log_prob_matrix.columns)
                for res in preds["Res_type"].unique():
                # For each amino acid type in turn:
                    # Work out which observations could be that aa type
                    allowed = obs[ss_class].str.contains(res).fillna(True)
                    # Select the predictions which are that aa type
                    pred_list = preds.loc[preds["Res_typem1"]==res,"Res_name"]
                    # For the selected predictions, penalise any observations 
                    # where the current aa type is not allowed
                    for p in pred_list:
                        SS_class_matrix.loc[:,p] = (~allowed)*log10(0.01)
            
                log_prob_matrix = log_prob_matrix + SS_class_matrix
                
            
        
        log_prob_matrix[log_prob_matrix.isna()] = 2*np.nanmin(
                                                        log_prob_matrix.values)
        log_prob_matrix.loc[obs["Dummy_SS"], :] = 0
        log_prob_matrix.loc[:, preds["Dummy_res"]] = 0
        
        self.log_prob_matrix = log_prob_matrix
        #return(preds_corr)
        return(self.log_prob_matrix)
    
    def calc_dist_matrix(self, use_atoms=None, atom_scale=None, na_dist=0, rank=False):
        """Calculate the Euclidian distance between each observation and 
        prediction.
        
        use_atoms: limit the set of atoms considered. If None, uses 
            pars["atom_set"]
        atom_scale: how much the shift difference is scaled by
        na_dist: shift differences which can't be calculated (eg due to 
            missing data) are replaced with this value
        rank: if True, returns the rank of the distance per observation
        """
        obs = self.obs
        preds = self.preds
        
        # Use default atom_sd values if not defined
        if atom_scale==None:
            atom_scale = self.pars["atom_sd"]
        
        if use_atoms==None:
            atoms = self.pars["atom_set"].intersection(obs.columns)
        else:
            atoms = set(use_atoms).intersection(obs.columns)
        
        delta2 = pd.DataFrame(0, index=obs.index, columns=preds.index)
        
        for atom in atoms:
            obs_atom = (obs[atom].repeat(len(obs.index)).values.
                        reshape([len(obs.index),-1]))
            preds_atom = (preds[atom].repeat(len(preds.index)).values.
                          reshape([len(preds.index),-1]).transpose())
            
            delta2_atom = ((preds_atom - obs_atom)/atom_scale[atom])**2
            
            # Make a note of NA positions in delta, and set them to default value 
            na_mask = np.isnan(delta2_atom)
            delta2_atom[na_mask] = na_dist
            
            delta2 = delta2 + delta2_atom
            
        dist_matrix = delta2.applymap(sqrt)
        
        if rank:
            return (dist_matrix.rank(axis=1))
        else:
            return(dist_matrix)
    
        return(assign_df, [row_ind, col_ind])
        
    def find_best_assignments(self, inc=None, exc=None):
        """ Use the Hungarian algorithm to find the highest probability matching 
        (ie. the one with the lowest log probability sum), with constraints.
        
        Returns a data frame with the SS_names and Res_names of the matching. 
        (Doesn't change the internal state of the NAPS_assigner instance.)
        
        inc: a DataFrame of (SS,Res) pairs which must be part of the assignment. 
            First column has the SS_names, second has the Res_names .
        exc: a DataFrame of (SS,Res) pairs which may not be part of the assignment.
        """
        obs = self.obs
        preds = self.preds
        log_prob_matrix = deepcopy(self.log_prob_matrix)
        
        if inc is not None:
            # Check for conflicting entries in inc
            conflicts = inc["SS_name"].duplicated(keep=False) | inc["Res_name"].duplicated(keep=False)
            if any(conflicts):
                print("Error: entries in inc conflict with one another - dropping conflicts")
                print(inc[conflicts])
                inc = inc[~conflicts]
            
            if exc is not None:
                # Check constraints are consistent
                # Get rid of any entries in exc which share a Res or SS with inc
                exc_in_inc = exc["SS_name"].isin(inc["SS_name"]) | exc["Res_name"].isin(inc["Res_name"])
                if any(exc_in_inc):
                    print("Some values in exc are also found in inc, so are redundant.")
                    exc = exc.loc[~exc_in_inc, :]
                    
            # Removed fixed assignments from probability matrix
            log_prob_matrix_reduced = log_prob_matrix.drop(index=inc["SS_name"]).drop(columns=inc["Res_name"])
        else:
            log_prob_matrix_reduced = log_prob_matrix
        
        if exc is not None:
            # Penalise excluded SS,Res pairs
            penalty = 2*log_prob_matrix.min().min()
            for index, row in exc.iterrows():
                # Need to account for dummy residues or spin systems
                if preds.loc[row["Res_name"], "Dummy_res"]:
                    log_prob_matrix_reduced.loc[row["SS_name"], 
                                    preds.loc[preds["Dummy_res"],"Res_name"]] = penalty
                elif obs.loc[row["SS_name"], "Dummy_SS"]:
                    log_prob_matrix_reduced.loc[obs.loc[obs["Dummy_SS"],"SS_name"], 
                                                row["Res_name"]] = penalty
                else:
                    log_prob_matrix_reduced.loc[row["SS_name"], row["Res_name"]] = penalty
        
        row_ind, col_ind = linear_sum_assignment(-1*log_prob_matrix_reduced)
        # -1 because the algorithm minimises sum, but we want to maximise it.
        
        # Construct results dataframe
        matching_reduced = pd.DataFrame({"SS_name":log_prob_matrix_reduced.index[row_ind],
                                           "Res_name":log_prob_matrix_reduced.columns[col_ind]})
        
        if inc is not None:
            matching = pd.concat([inc, matching_reduced])             
            return(matching)
        else:
            return(matching_reduced)
    
    def make_assign_df(self, matching, set_assign_df=False):
        """Make a dataframe with full assignment information, given a dataframe 
        of SS_name and Res_name.
        
        Matching may have additional columns, which will also be kept.
        """
        obs = self.obs
        preds = self.preds
        log_prob_matrix = self.log_prob_matrix
        valid_atoms = list(self.pars["atom_set"])
        extra_cols = set(matching.columns).difference({"SS_name","Res_name"})
        
        assign_df = pd.merge(matching, 
                             preds.loc[:,["Res_N","Res_type", "Res_name", 
                                    "Dummy_res"]], 
                             on="Res_name", how="left")
        assign_df = assign_df[["Res_name","Res_N","Res_type","SS_name", 
                               "Dummy_res"]+list(extra_cols)]
        assign_df = pd.merge(assign_df, 
                             obs.loc[:, obs.columns.isin(
                                     ["SS_name","Dummy_SS"]+valid_atoms)], 
                             on="SS_name", how="left")
        assign_df = pd.merge(assign_df, 
                             preds.loc[:, preds.columns.isin(
                                     valid_atoms+["Res_name"])],
                             on="Res_name", suffixes=("","_pred"), how="left")
        
        assign_df["Log_prob"] = log_prob_matrix.lookup(
                                            assign_df["SS_name"],
                                            assign_df["Res_name"])
        # Careful above not to get rows/columns confused
        
        assign_df = assign_df.sort_values(by="Res_N")
        
        if set_assign_df:
            self.assign_df = assign_df
        
        return(assign_df)
        
    
    def check_assignment_consistency(self, assign_df=None, threshold=0.1):
        """ Find maximum mismatch and number of 'significant' mismatches for 
        each residue
        
        threshold: Minimum carbon shift difference for sequential residues to
            count as mismatched
        """
        
        # If the user hasn't specified an assign_df, use one already calculated 
        # for this NAPS_assigner instance
        if assign_df is None:
            set_assign_df = True
            assign_df = self.assign_df
        else:
            set_assign_df = False
        
        # First check if there are any sequential atoms
        carbons = pd.Series(["C","CA","CB"])
        carbons_m1 = carbons + "m1"
        seq_atoms = carbons[carbons.isin(assign_df.columns) & 
                            carbons_m1.isin(assign_df.columns)]
        seq_atoms_m1 = seq_atoms+"m1"
        #seq_atoms = list(seq_atoms)
    
        if seq_atoms.size==0:
            # You can't do a comparison
            assign_df["Max_mismatch_prev"] = np.NaN
            assign_df["Max_mismatch_next"] = np.NaN
            assign_df["Num_good_links_prev"] = np.NaN
            assign_df["Num_good_links_next"] = np.NaN
            return(assign_df)
        else:
            # First, get the i and i-1 shifts for the preceeding and 
            # succeeding residues
            tmp = assign_df.copy()
            tmp = tmp.loc[tmp["Dummy_res"]==False,]
            tmp.index = tmp["Res_N"]
            tmp = tmp[list(seq_atoms)+list(seq_atoms_m1)]
            tmp_next = tmp.copy()
            tmp_next.index -= 1
            tmp_prev = tmp.copy()
            tmp_prev.index += 1
            tmp = tmp.join(tmp_next, rsuffix="_next")
            tmp = tmp.join(tmp_prev, rsuffix="_prev")
            # Calculate mismatch for each atom type
            for atom in seq_atoms:
                tmp["d"+atom+"_prev"] = tmp[atom+"m1"] - tmp[atom+"_prev"]
                tmp["d"+atom+"_next"] = tmp[atom] - tmp[atom+"m1_next"]
            # Calculate maximum mismatch
            tmp["Max_mismatch_prev"] = tmp["d"+seq_atoms+"_prev"].max(axis=1, 
                                                                   skipna=True)
            tmp["Max_mismatch_next"] = tmp["d"+seq_atoms+"_next"].max(axis=1,
                                                                   skipna=True)
            
            # Calculate number of consistent matches
            tmp["Num_good_links_prev"] = (tmp["d"+seq_atoms+"_prev"]<threshold).sum(axis=1)
            tmp["Num_good_links_next"] = (tmp["d"+seq_atoms+"_next"]<threshold).sum(axis=1)
            
            # Join relevant columns back onto assign_df
            tmp["Res_N"] = tmp.index
            assign_df = assign_df.join(tmp.loc[:,["Max_mismatch_prev", 
                                                  "Max_mismatch_next", 
                                                  "Num_good_links_prev", 
                                                  "Num_good_links_next"]], 
                                       on="Res_N")
            if set_assign_df:
                self.assign_df = assign_df
            return(assign_df)
        
    def find_alt_assignments(self, N=1, by_ss=True, verbose=False):
        """ Find the next-best assignment(s) for each residue or spin system
        
        This works by setting the log probability to a very high value for each 
        residue in turn, and rerunning the assignment
        
        Arguments:
        best_match_indexes: [row_ind, col_ind] output from find_best_assignment()
        N: number of alternative assignments to generate
        by_ss: if true, calculate next best assignment for each spin system. 
            Otherwise, calculate it for each residue.
        
        Output:
        A Dataframe containing the original assignments, and the 
        alt_assignments by
        """
        
        obs = self.obs
        preds = self.preds
        log_prob_matrix = self.log_prob_matrix
        #best_match_indexes = self.best_match_indexes
        best_matching = self.assign_df.loc[:,["SS_name","Res_name"]]
        best_matching.index = best_matching["SS_name"]
        alt_matching = None
        
        # Calculate sum probability for the best matching
        best_sum_prob = sum(log_prob_matrix.lookup(
                best_matching["SS_name"], best_matching["Res_name"]))
        
        # Calculate the value used to penalise the best match for each residue
        penalty = 2*log_prob_matrix.min().min()     
        logging.debug("Penalty value: %f", penalty)
        
        # Initialise DataFrame for storing alt_assignments
        alt_matching_all = best_matching.copy()
        alt_matching_all["Rank"] = 1
        alt_matching_all["Rel_prob"] = 0
               
        
        for i in best_matching.index:   # Consider each spin system in turn
            ss = best_matching.loc[i, "SS_name"]
            res = best_matching.loc[i, "Res_name"]
            logging.debug("Finding alt assignments for original match %s - %s", ss, res)
            if verbose: print(ss, res)
            
            excluded = best_matching.loc[[i], :]
            
            for j in range(N):
                alt_matching = self.find_best_assignment2(exc=excluded)
                                
                alt_matching["Rank"] = j+2
                alt_sum_prob = sum(self.log_prob_matrix.lookup(
                        alt_matching["SS_name"], alt_matching["Res_name"]))
                alt_matching["Rel_prob"] = alt_sum_prob - best_sum_prob
                
                
                # Add the alt match for this ss or res to the results dataframe 
                # and also the excluded dataframe.
                if by_ss:
                    alt_matching_all = alt_matching_all.append(
                            alt_matching.loc[alt_matching["SS_name"]==ss, :], 
                            ignore_index=True)
                    res = alt_matching.loc[alt_matching["SS_name"]==ss, 
                                           "Res_name"].tolist()[0]
                    # The .tolist()[0] is to convert a single-item series into a string.
                else:
                    alt_matching_all = alt_matching_all.append(
                            alt_matching.loc[alt_matching["Res_name"]==res, :], 
                            ignore_index=True)
                    ss = alt_matching.loc[alt_matching["Res_name"]==res, 
                                          "SS_name"].tolist()[0]
                excluded = excluded.append(pd.DataFrame({"SS_name":[ss],"Res_name":[res]}), 
                                           ignore_index=True)
                   
        self.alt_assign_df = self.make_assign_df(alt_matching_all)
        if by_ss:
            self.alt_assign_df = self.alt_assign_df.sort_values(
                                                by=["SS_name", "Rank"])
        else:
            self.alt_assign_df = self.alt_assign_df.sort_values(
                                                by=["Res_name", "Rank"])
            
        return(self.alt_assign_df)
    
    def output_peaklists(self, filepath, format="sparky", 
                         spectra=["hsqc","hnco","hncaco","hncacb", "hncocacb"]):
        """ Output assigned peaklists
        """
        return(0)
    
    def plot_strips(self, atom_list=["C","Cm1","CA","CAm1","CB","CBm1"]):
        """ Make a strip plot of the assignment
        
        atom_list: only plot data for these atom types
        """
        assign_df = self.assign_df
        
        # Narrow down atom list to those actually present
        atom_list = list(set(atom_list).intersection(assign_df.columns))
        
        # First, convert assign_df from wide to long
        plot_df = assign_df.loc[:,["Res_N", "Res_type", "Res_name", "SS_name", 
                                   "Dummy_res", "Dummy_SS"]+atom_list]
        plot_df = plot_df.melt(id_vars=["Res_N", "Res_type", "Res_name", 
                                        "SS_name", "Dummy_res", "Dummy_SS"],
                                   value_vars=atom_list, var_name="Atom_type",
                                   value_name="Shift")
        
        # Add columns with information to be plotted
        plot_df["i"] = "0"     # Track if shift is from the i or i-1 residue
        plot_df.loc[plot_df["Atom_type"].isin(["Cm1","CAm1","CBm1"]),"i"] = "-1"
        plot_df["Atom_type"] = plot_df["Atom_type"].replace({"Cm1":"C", 
                                                   "CAm1":"CA", "CBm1":"CB"}) 
                                                    # Simplify atom type
        
        plot_df["seq_group"] = plot_df["Res_N"] + plot_df["i"].astype("int")
        
        # Pad Res_name column with spaces so that sorting works correctly
        plot_df["Res_name"] = plot_df["Res_name"].str.pad(6)
        plot_df["x_name"] = plot_df["Res_name"] + "_(" + plot_df["SS_name"] + ")"
        
        # Make the plot
        plt = ggplot(aes(x="x_name"), data=plot_df) 
        plt = plt + geom_point(aes(y="Shift", colour="i", shape="Dummy_res"))
        plt = plt + scale_y_reverse() + scale_shape_manual(values=["o","x"])
        # Add lines connecting i to i-1 points
        plt = plt + geom_line(aes(y="Shift", group="seq_group"), 
                              data=plot_df.loc[~plot_df["Dummy_res"],])        
        plt = plt + geom_line(aes(y="Shift", group="x_name"), linetype="dashed")
        plt = plt + facet_grid("Atom_type~.", scales="free") 
        plt = plt + scale_colour_brewer(type="Qualitative", palette="Set1") 
        plt = plt + xlab("Residue name") + ylab("Chemical shift (ppm)")
        plt = plt + theme_bw() + theme(axis_text_x = element_text(angle=90))
        
        return(plt)
    
    def plot_seq_mismatch(self):
        """ Make a plot of the maximum sequential mismatch between i-1, i and 
        i+1 residues
        """
        assign_df = self.assign_df
        
        # Check that the assignment data frame has the right columns
        if not all(pd.Series(['Max_mismatch_prev', 'Max_mismatch_next']).
                   isin(assign_df.columns)):
            return(None)
        else:
            # Pad Res_name column with spaces so that sorting works correctly
            assign_df["Res_name"] = assign_df["Res_name"].str.pad(6)
            assign_df["x_name"] = (assign_df["Res_name"] + "_(" + 
                                     assign_df["SS_name"] + ")")
            
            # Make the plot
            plt = ggplot(aes(x="x_name"), data=assign_df) 
            plt = plt + geom_col(aes(y="abs(Max_mismatch_prev)"))
            plt = plt + xlab("Residue name")
            plt = plt + ylab("Mismatch to previous residue (ppm)")
            plt = plt + theme_bw() + theme(axis_text_x = element_text(angle=90))
                   
            return(plt)

#%%
        
#### Testing 

#a = NAPS_assigner()
#a.import_obs_shifts("~/GitHub/NAPS/data/testset/simplified_BMRB/6338.txt")
#a.import_pred_shifts("~/GitHub/NAPS/data/P3a_L273R/shiftx2.cs", offset=208)
#a.add_dummy_rows()
#a.calc_log_prob_matrix(sf=2, verbose=False)
#assign_df, best_match_indexes = a.find_best_assignment()


## Import the observed and predicted shifts
#    obs = import_obs_shifts(obs_file)
#    preds = import_pred_shifts(preds_file)
#    
#    # Add dummy rows so that obs and preds are the same length
#    obs, preds = add_dummy_rows(obs, preds)
#    
#    # Calculate the log probability for each observation/prediction pair
#    log_prob_matrix = calc_log_prob_matrix(obs, preds, sf=2)
#    
#    # Find the assignment with the highest overall probability
#    assign_df, matching = find_best_assignment(obs, preds, log_prob_matrix)        
        
        