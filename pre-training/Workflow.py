# load general packages and functions
import numpy as np
import pickle
import shutil
import time
import torch
import torch.utils.tensorboard
from tqdm import tqdm
import os
from torch import nn
from rdkit import Chem

# load program-specific functions
import analyze as anal
import preprocessing as prep
from BlockDatasetLoader import BlockDataLoader, HDFDataset,BlockDataFragmentLoader,HDFFragmentDataset
import generate
import loss
import models
import util



# set default torch dtype
torch.set_default_dtype(torch.float32)

class Workflow:
    """ Single `Workflow` class split up into different functions for
      1) preprocessing various molecular datasets
      2) training generative models
      3) generating molecules using pre-trained models
      4) evaluating generative models

    The preprocessing step reads a set of molecules and generates training data
    for each molecule in HDF file format, consisting of decoding routes and
    APDs. During training, the decoding routes and APDs are used to train graph
    neural network models to generate new APDs, from which actions are
    stochastically sampled and used to build new molecular graphs. During
    generation, a pre-trained model is used to generate a fixed number of
    structures. During evaluation, metrics are calculated for the test set.
    """
    def __init__(self, constants):

        self.start_time = time.time()

        self.C = constants

        # define path variables for various datasets
        self.test_h5_path = self.C.test_set[:-3] + "h5"
        self.train_h5_path = self.C.training_set[:-3] + "h5"
        self.valid_h5_path = self.C.validation_set[:-3] + "h5"
        self.train_fragment_h5_path = self.C.training_fragment_set[:-3] + "h5"
        self.test_fragment_h5_path = self.C.test_fragment_set[:-3] + "h5"
        self.valid_fragment_h5_path = self.C.validation_fragment_set[:-3] + "h5"

        # create placeholders
        self.model = None
        self.optimizer = None

        self.test_dataloader = None
        self.train_dataloader = None
        self.valid_dataloader = None

        self.ts_properties = None
        self.current_epoch = None
        self.restart_epoch = None
        self.nll_per_action = None

        self.tensorboard_writer = None

        self.n_subgraphs = None

    def preprocess_test_data(self):
        """ Converts test dataset to HDF file format.
        """
        print("* Preprocessing test data.", flush=True)
        prep.create_HDF_file(self.C.test_set)

        self.print_time_elapsed()

    def preprocess_train_fragment_data(self):
        print("* Preprocessing train_fragment data.", flush=True)
        prep.create_fragment_HDF_file(self.C.training_fragment_set)

        self.print_time_elapsed()

    def preprocess_test_fragment_data(self):
        print("* Preprocessing test_fragment data.", flush=True)
        prep.create_fragment_HDF_file(self.C.test_fragment_set)

        self.print_time_elapsed()

    def preprocess_valid_fragment_data(self):
        print("* Preprocessing valid_fragment data.", flush=True)
        prep.create_fragment_HDF_file(self.C.validation_fragment_set)

        self.print_time_elapsed()

    def preprocess_train_data(self):
        """ Converts training dataset to HDF file format.
        """
        print("* Preprocessing training data.", flush=True)
        prep.create_HDF_file(self.C.training_set, is_training_set=True)

        self.print_time_elapsed()

    def preprocess_valid_data(self):
        """ Converts validation dataset to HDF file format.
        """
        print("* Preprocessing validation data.", flush=True)
        prep.create_HDF_file(self.C.validation_set)

        self.print_time_elapsed()

    def get_dataloader(self, hdf_path, data_description=None,fragment=False):
        """ Loads preprocessed data as `torch.utils.data.DataLoader` object.

        Args:
          data_path (str) : Path to HDF data to be read.
          data_description (str) : Used for printing status (e.g. "test data").
        """
        if data_description is None:
            data_description = "data"
        print(f"* Loading preprocessed {data_description}.", flush=True)
        if fragment:
            dataset = HDFFragmentDataset(hdf_path)
        else:

            dataset = HDFDataset(hdf_path)
        fragment_dataset = HDFFragmentDataset(hdf_path)
        if data_description == "training set":
            self.n_subgraphs = len(dataset)
        if fragment:
            dataloader = BlockDataFragmentLoader(dataset=dataset,
                                     batch_size=self.C.batch_size,
                                     block_size=self.C.block_size,
                                     shuffle=True,
                                     n_workers=self.C.n_workers,
                                     pin_memory=True)
        else:
            dataloader = BlockDataLoader(dataset=dataset,
                                     batch_size=self.C.batch_size,
                                     block_size=self.C.block_size,
                                     shuffle=True,
                                     n_workers=self.C.n_workers,
                                     pin_memory=True)
        self.print_time_elapsed()

        return dataloader

    def get_ts_properties(self):
        """ Loads the training sets properties from CSV as a dictionary, properties
        are used later for model evaluation.
        """
        filename = self.C.training_set[:-3] + "csv"
        self.ts_properties = util.load_ts_properties(csv_path=filename)

    def define_model_and_optimizer(self):
        """ Defines the model (`self.model`) and the optimizer (`self.optimizer`).
        """
        print("* Defining model and optimizer.", flush=True)
        job_dir = self.C.job_dir

        if self.C.restart:
            print("-- Loading model from previous saved state.", flush=True)
            self.restart_epoch = util.get_restart_epoch()
            self.model = torch.load(f"{job_dir}model_restart_{self.restart_epoch}.pth")

            print(
                f"-- Backing up as "
                f"{job_dir}model_restart_{self.restart_epoch}_restarted.pth.",
                flush=True,
            )
            shutil.copyfile(
                f"{job_dir}model_restart_{self.restart_epoch}.pth",
                f"{job_dir}model_restart_{self.restart_epoch}_restarted.pth",
            )

        else:
            print("-- Initializing model from scratch.", flush=True)
            self.model= models.initialize_model()

            self.restart_epoch = 0

        start_epoch = self.restart_epoch + 1
        end_epoch = start_epoch + self.C.epochs
        

        print("-- Defining optimizer.", flush=True)
        self.optimizer = torch.optim.Adam(
            params=self.model.parameters(),
            lr=self.C.init_lr,
            weight_decay=self.C.weight_decay,
        )

        print("-- Defining scheduler.", flush=True)
        self.scheduler = torch.optim.lr_scheduler.OneCycleLR(
            optimizer=self.optimizer,
            max_lr= self.C.max_rel_lr * self.C.init_lr,
            div_factor= 1. / self.C.max_rel_lr,
            final_div_factor = 1. / self.C.min_rel_lr,
            pct_start = 0.05,
            total_steps=self.C.epochs * (self.n_subgraphs // self.C.batch_size + 1),
            epochs=self.C.epochs
        )

        return start_epoch, end_epoch

    def preprocess_phase(self):
        """ Preprocesses all the datasets (validation, training, and testing).
        """
        if not self.C.restart:  # start preprocessing job from scratch
            if (
                os.path.exists(self.train_h5_path)
                or os.path.exists(self.test_h5_path)
                or os.path.exists(self.valid_h5_path)
            ):
                raise OSError(
                    f"There currently exist(s) pre-created *.h5 file(s) in the "
                    f"dataset directory. If you would like to proceed with "
                    f"creating new ones, please delete them and rerun the "
                    f"program. Otherwise, check your input file."
                )
            self.preprocess_train_fragment_data()
            self.preprocess_test_fragment_data()
            self.preprocess_valid_fragment_data()
            self.preprocess_valid_data()
            self.preprocess_test_data()
            self.preprocess_train_data()
        else:  # restart existing preprocessing job
            # as some datasets may have already been preprocessed, check for this
            if os.path.exists(self.train_h5_path + ".chunked") or os.path.exists(self.test_h5_path):
                print(
                    f"-- Restarting preprocessing job from 'train.h5' "
                    f"(skipping over 'test.h5' and 'valid.h5' as they seem "
                    f"to be finished).",
                    flush=True,
                )
                self.preprocess_train_data()
            elif os.path.exists(self.test_h5_path + ".chunked") or os.path.exists(self.valid_h5_path):
                print(
                    f"-- Restarting preprocessing job from 'test.h5' "
                    f"(skipping over 'valid.h5' as it appears to be "
                    f"finished).",
                    flush=True,
                )
                self.preprocess_test_data()
                self.preprocess_train_data()
            elif os.path.exists(self.valid_h5_path + ".chunked"):
                print(f"-- Restarting preprocessing job from 'valid.h5'", flush=True)
                self.preprocess_valid_data()
                self.preprocess_test_data()
                self.preprocess_train_data()
            else:
                raise ValueError(
                    "Warning: Nothing to restart! Check input "
                    "file and/or submission script."
                )

    def training_phase(self):
        """ Trains model (`self.model`) and generates graphs.
        """
        self.train_dataloader = self.get_dataloader(
            hdf_path=self.train_h5_path,
            data_description="training set"
        )#may random
        self.valid_dataloader = self.get_dataloader(
            hdf_path=self.valid_h5_path,
            data_description="validation set"
        )
        self.train_fragment_dataloader = self.get_dataloader(
            hdf_path=self.train_fragment_h5_path,
            data_description = 'training fragment set',
            fragment=True

        )
        self.valid_fragment_dataloader = self.get_dataloader(
            hdf_path=self.valid_fragment_h5_path,
            data_description = 'valid fragment set',
            fragment = True
        )



        self.get_ts_properties()

        self.initialize_output_files()

        start_epoch, end_epoch = self.define_model_and_optimizer()
        

        print("* Beginning training.", flush=True)
        n_processed_batches = 0
        for epoch in range(start_epoch, end_epoch):

            self.current_epoch = epoch
            n_processed_batches = self.train_epoch(n_processed_batches=n_processed_batches)

            # evaluate model every `sample_every` epochs (not every epoch)
            if epoch % self.C.sample_every == 0:
                self.evaluate_model()
            else:
                util.write_model_status(score="NA")  # score not computed

        self.print_time_elapsed()

    def generation_phase(self):
        """ Generates molecules from a pre-trained model.
        """
        self.get_ts_properties()

        self.restart_epoch = self.C.generation_epoch
        print(f"* Loading model from previous saved state (Epoch {self.restart_epoch}).", flush=True)
        model_path = self.C.job_dir + f"model_restart_{self.restart_epoch}.pth"
        self.model = torch.load(model_path)

        self.model.eval()
        with torch.no_grad():
            self.generate_linker_fragments_graphs(n_samples=self.C.n_samples)

        self.print_time_elapsed()


    def generate_linker_fragments_graphs(self, n_samples, evaluation=False, epoch_key=None):
        """ Generates `n_graphs` molecular graphs and evaluates them. Generates
        the graphs in batches the size of `self.C.batch_size` or `n_samples` (int),
        whichever is smaller.
        """
        print(f"* Generating {n_samples} molecules.", flush=True)

        generation_batch_size = min(self.C.batch_size, n_samples)

        n_generation_batches = int(n_samples/self.C.batch_size)
        if n_samples % self.C.batch_size != 0:
            n_generation_batches += 1

        # generate graphs in batches
        for idx in range(0, n_generation_batches):
            print("Batch", idx+1, "of", n_generation_batches)

            # generate one batch of graphs
            # g : generated graphs (list of `GenerationGraph`s)
            # a : action NLLs (torch.Tensor)
            # f : final NLLs (torch.Tensor)
            # t : termination status (torch.Tensor)
            g, a, f, t,_,_,_,two_idx = generate.build_graphs(model=self.model,
                                               n_graphs_to_generate=generation_batch_size,
                                               batch_size=generation_batch_size)

            # analyze properties of new graphs and save results
            smi_list = []
            from rdkit import Chem
            from rdkit.Chem import AllChem
            import random

            for idxm, molecular_graph in enumerate(g):

                mol = molecular_graph.get_molecule()
                smi = Chem.MolToSmiles(mol)
                smi_list.append(smi)
                try:
                    mol.UpdatePropertyCache(strict=False)
                    Chem.SanitizeMol(mol)
                    
                    
                except (ValueError, RuntimeError, AttributeError):
                    pass
            for id,atom_idx in enumerate(two_idx):
                try:
                    fragments_smi = self.C.generate_fragments
                    fragments = Chem.MolFromSmiles(fragments_smi)
                    linker_smi_1 = smi_list[id]
                    
                    linker = Chem.MolFromSmiles(linker_smi_1)
                    #connect
                    combo = Chem.CombineMols(fragments,linker)
                    idx_list = []
                    for atom in fragments.GetAtoms():
                        if atom.GetSymbol() == '*':
                            f2 = atom.GetIdx()
                            idx_list.append(f2)

                    du = Chem.MolFromSmiles('*')
                    combo = AllChem.DeleteSubstructs(combo,du)
                    edcombo = Chem.EditableMol(combo)
                    try:
                        edcombo.AddBond(idx_list[0],atom_idx[0]+idx_list[1]-2,order=Chem.rdchem.BondType.SINGLE)
                        edcombo.AddBond(idx_list[1]-2,atom_idx[1]+idx_list[1]-2,order=Chem.rdchem.BondType.SINGLE)
                        final_connect_mol = edcombo.GetMol()
                    except:
                
                        fragment_smi_0 = fragments_smi.split('.')[0]
                        fragment_smi_1 = fragments_smi.split('.')[1]
                        
                        match = '****sfs'
                        for _ in range(1000):
                            if not Chem.MolFromSmiles(match):
                                s = linker_smi_1
                                inStr = "(*)"
                                for i in range(2):
                                    index = random.randint(0, len(s))
                                    s = "".join([s[:index], inStr, s[index:]])
                                match = s
                            else:
                                break
                        smi_linker = []
                        for _ in range(1000):
                            smi_link = Chem.MolToSmiles(Chem.MolFromSmiles(s), doRandom=True)
                            if smi_link[0] == "*" and smi_link[-1] == '*':
                                smi_linker.append(smi_link)
                        
                            
                        final_smi = (fragment_smi_1 + smi_linker[0] + fragment_smi_0).replace("*", "")

                        final_connect_mol = Chem.MolFromSmiles(final_smi)
                        
                    final_connect_smi = Chem.MolToSmiles(final_connect_mol)

                except:
                    fragment_smi_0 = fragments_smi.split('.')[0]
                    fragment_smi_1 = fragments_smi.split('.')[1]
                        
                    final_connect_smi = (fragment_smi_1 + fragment_smi_0).replace("*", "")

                    
                smi_filename = self.C.job_dir + f"generation/generation_epoch{fragments_smi}.smi"
                f9 = open(smi_filename,'a')
                try:
                    f9.write(final_connect_smi+'\n')
                    f9.close()
                except:
                    f9.write('C'+'\n')
                    f9.close()


            # keep track of NLLs per action if `evaluation`==True
            # note that only NLLs for the first batch are kept, as only a few
            # are needed to evaluate the model (more efficient than saving all)
            if evaluation and idx == 0:
                self.nll_per_action = a
    def testing_phase(self):
        """ Evaluates model using test set data.
        """
        self.test_dataloader = self.get_dataloader(self.test_h5_path, "test set")
        self.get_ts_properties()

        self.restart_epoch = util.get_restart_epoch()
        print(f"* Loading model from previous saved state (Epoch {self.restart_epoch}).", flush=True)
        self.model = torch.load(
            self.C.job_dir + f"model_restart_{self.restart_epoch}.pth"
        )

        self.model.eval()
        with torch.no_grad():
            self.generate_graphs(n_samples=self.C.n_samples)

            print("* Evaluating model.", flush=True)
            anal.evaluate_model(valid_dataloader=self.test_dataloader,
                                train_dataloader=self.train_dataloader,
                                nll_per_action=self.nll_per_action,
                                model=self.model)

        self.print_time_elapsed()

    def multiple_valid_phase(self):
        """ Evaluates model using test set data.
        """
        self.train_dataloader = self.get_dataloader(
            hdf_path=self.train_h5_path,
            data_description="training set"
        )
        self.valid_dataloader = self.get_dataloader(
            hdf_path=self.valid_h5_path,
            data_description="validation set"
        )
        self.get_ts_properties()

        for epoch in self.C.generation_epoch:
            self.restart_epoch = epoch
            print(f"* Loading model from previous saved state (Epoch {self.restart_epoch}).", flush=True)
            self.model = torch.load(self.C.job_dir + f"model_restart_{self.restart_epoch}.pth")

            self.model.eval()
            with torch.no_grad():
                self.generate_graphs(n_samples=self.C.n_samples, epoch_key = f"Epoch REEVAL{self.restart_epoch}")

            self.print_time_elapsed()

    def evaluate_model(self):
        """ Evaluates model by calculating the UC-JSD from generated structures.
        Saves model scores in `validation.csv` and then saves model state.
        """
        self.model.eval()      # sets layers to eval mode (e.g. norm, dropout)
        with torch.no_grad():  # deactivates autograd engine

            # generate graphs required for model evaluation
            # note that evaluation of the generated graphs happens in
            # `generate_graphs()`, and molecules are saved as `self` attributes
            self.generate_graphs(n_samples=self.C.n_samples, evaluation=True)

            print("* Evaluating model.", flush=True)
            anal.evaluate_model(valid_dataloader=self.valid_dataloader,
                                train_dataloader=self.train_dataloader,
                                nll_per_action=self.nll_per_action,
                                model=self.model)

            self.nll_per_action = None  # don't need anymore

            self.compute_valid_loss_epoch()

            print(f"* Saving model state at Epoch {self.current_epoch}.", flush=True)

            # `pickle.HIGHEST_PROTOCOL` good for large objects
            model_path_and_filename = (self.C.job_dir + f"model_restart_{self.current_epoch}.pth")
            torch.save(obj=self.model,
                       f=model_path_and_filename,
                       pickle_protocol=pickle.HIGHEST_PROTOCOL)


    def compute_valid_loss_epoch(self):
        
        loss_tensor = torch.zeros(len(self.valid_dataloader), device="cuda")

        # each batch consists of `batch_size` molecules
        # **note: "idx" == "index"
        for batch_idx in tqdm(range(len(self.valid_dataloader))):
            
            
            _,batch_linker = list(enumerate(self.valid_dataloader))[batch_idx]
            _,batch_fragment = list(enumerate(self.valid_fragment_dataloader))[1]
            

       
            batch_linker = [b.cuda(non_blocking=True) for b in batch_linker]
            batch_fragment = [b.cuda(non_blocking=True) for b in batch_fragment]
            nodes_linker, edges_linker, target_output = batch_linker
            nodes_fragment, edges_fragment = batch_fragment
                
                    

            # return the output
            output,linker_loss,_ = self.model(nodes_linker, edges_linker,nodes_fragment,edges_fragment)


            # compute the loss
            batch_loss = loss.graph_generation_loss(
                output=output,
                target_output=target_output,
            )

            loss_tensor[batch_idx] = batch_loss

        with open(self.C.job_dir + "valid-loss.csv", "a") as output_file:
            output_file.write(f"{self.current_epoch} \t {torch.mean(loss_tensor)}\n")

        self.print_time_elapsed()

    def compute_valid_loss(self):
        self.valid_dataloader = self.get_dataloader(
            hdf_path=self.valid_h5_path,
            data_description="validation set"
        )

        with open(self.C.job_dir + "valid-loss.csv", "a") as output_file:
            output_file.write(f"Epoch \t Valid loss\n")
        

        for epoch in self.C.generation_epoch:
            self.restart_epoch = epoch
            print(f"* Loading model from previous saved state (Epoch {self.restart_epoch}).", flush=True)
            self.model = torch.load(self.C.job_dir + f"model_restart_{self.restart_epoch}.pth")
            

            self.model.eval()
            with torch.no_grad():
                loss_tensor = torch.zeros(len(self.valid_dataloader), device="cuda")
        

                # each batch consists of `batch_size` molecules
                # **note: "idx" == "index"

                for batch_idx in tqdm(range(len(self.valid_dataloader))):
            
            
                    _,batch_linker = list(enumerate(self.valid_dataloader))[batch_idx]
                    _,batch_fragment = list(enumerate(self.valid_fragment_dataloader))[1]
                    

                # for batch_idx, batch in tqdm(
                #     enumerate(self.train_dataloader), total=len(self.train_dataloader)
                # ):
                    
                    batch_linker = [b.cuda(non_blocking=True) for b in batch_linker]
                    batch_fragment = [b.cuda(non_blocking=True) for b in batch_fragment]
                    nodes_linker, edges_linker, target_output = batch_linker
                    nodes_fragment, edges_fragment = batch_fragment
                
                    

                    # return the output
                    output,linker_loss,_ = self.model(nodes_linker, edges_linker,nodes_fragment,edges_fragment)

                    # compute the loss
                    batch_loss = loss.graph_generation_loss(
                        output=output,
                        target_output=target_output,
                    )

                    loss_tensor[batch_idx] = batch_loss

            with open(self.C.job_dir + "valid-loss.csv", "a") as output_file:
                output_file.write(f"{epoch} \t {torch.mean(loss_tensor)}\n")

            self.print_time_elapsed()



    def initialize_output_files(self):
        """ Creates output files (with appropriate headers) for new (i.e.
        non-restart) jobs. If restart a job, and all new output will be appended
        to existing output files.
        """
        if not self.C.restart:
            print("* Touching output files.", flush=True)
            # begin writing `generation.csv` file
            csv_path_and_filename = self.C.job_dir + "generation.csv"
            util.properties_to_csv(
                prop_dict=self.ts_properties,
                csv_filename=csv_path_and_filename,
                epoch_key="Training set",
                append=False,
            )

            with open(self.C.job_dir + "valid-loss.csv", "w") as output_file:
                output_file.write(f"Epoch \t Valid loss\n")

            # begin writing `convergence.csv` file
            util.write_model_status(append=False)

            # create `generation/` subdirectory to write generation output to
            os.makedirs(self.C.job_dir + "generation/", exist_ok=True)
    def train_generate_linker_graphs(self, n_samples, evaluation=False, epoch_key=None):
        generation_batch_size = min(self.C.batch_size, n_samples)

        n_generation_batches = int(n_samples/self.C.batch_size)
        if n_samples % self.C.batch_size != 0:
            n_generation_batches += 1

        # generate graphs in batches
        for idx in range(0, n_generation_batches):
            print("Batch", idx+1, "of", n_generation_batches)

            # generate one batch of graphs
            # g : generated graphs (list of `GenerationGraph`s)
            # a : action NLLs (torch.Tensor)
            # f : final NLLs (torch.Tensor)
            # t : termination status (torch.Tensor)
            g, a, f, t ,_,_,_,_= generate.build_graphs(model=self.model,
                                               n_graphs_to_generate=generation_batch_size,
                                               batch_size=generation_batch_size)
                                               



    def generate_graphs(self, n_samples, evaluation=False, epoch_key=None):
        """ Generates `n_graphs` molecular graphs and evaluates them. Generates
        the graphs in batches the size of `self.C.batch_size` or `n_samples` (int),
        whichever is smaller.
        """
        print(f"* Generating {n_samples} molecules.", flush=True)

        generation_batch_size = min(self.C.batch_size, n_samples)

        n_generation_batches = int(n_samples/self.C.batch_size)
        if n_samples % self.C.batch_size != 0:
            n_generation_batches += 1

        # generate graphs in batches
        for idx in range(0, n_generation_batches):
            print("Batch", idx+1, "of", n_generation_batches)

            # generate one batch of graphs
            # g : generated graphs (list of `GenerationGraph`s)
            # a : action NLLs (torch.Tensor)
            # f : final NLLs (torch.Tensor)
            # t : termination status (torch.Tensor)
            g, a, f, t,_,_,_,_ = generate.build_graphs(model=self.model,
                                               n_graphs_to_generate=generation_batch_size,
                                               batch_size=generation_batch_size)

            # analyze properties of new graphs and save results
            
            anal.evaluate_generated_graphs(generated_graphs=g,
                                           termination=t,
                                           nlls=f,
                                           start_time=self.start_time,
                                           ts_properties=self.ts_properties,
                                           generation_batch_idx=idx,
                                           epoch_key=epoch_key)

            # keep track of NLLs per action if `evaluation`==True
            # note that only NLLs for the first batch are kept, as only a few
            # are needed to evaluate the model (more efficient than saving all)
            if evaluation and idx == 0:
                self.nll_per_action = a

    def print_time_elapsed(self):
        """ Prints elapsed time since input `start_time`.
        """
        stop_time = time.time()
        elapsed_time = stop_time - self.start_time
        print(f"-- time elapsed: {elapsed_time:.5f} s", flush=True)

    def train_epoch(self, n_processed_batches=0):
        """ Performs one training epoch.
        """
        print(f"* Training epoch {self.current_epoch}.", flush=True)
        loss_tensor = torch.zeros(len(self.train_dataloader), device="cuda")
        

        self.model.train()  # ensure model is in train mode
        train_fragment_filename = '/train_fragment.smi'
        train_groundtruth_filename = '/train_groundtruth.smi'

        f1 = open(train_fragment_filename)
        trainfragment_datalist = [smi.rstrip() for smi in f1]
        f2 = open(train_groundtruth_filename)
        traingroundtruth_datalist = [smi.rstrip() for smi in f2]

       

        # each batch consists of `batch_size` molecules
        # **note: "idx" == "index"


        
        
        for batch_idx in tqdm(range(len(self.train_dataloader))):
            
            
            _,batch_linker = list(enumerate(self.train_dataloader))[batch_idx]
            _,batch_fragment = list(enumerate(self.train_fragment_dataloader))[1]
            

        # for batch_idx, batch in tqdm(
        #     enumerate(self.train_dataloader), total=len(self.train_dataloader)
        # ):
            n_processed_batches += 1
            batch_linker = [b.cuda(non_blocking=True) for b in batch_linker]
            batch_fragment = [b.cuda(non_blocking=True) for b in batch_fragment]
            nodes_linker, edges_linker, target_output = batch_linker
            nodes_fragment, edges_fragment = batch_fragment
            
            
            fragment_smi_list = trainfragment_datalist[batch_idx*self.C.batch_size : (batch_idx+1)*self.C.batch_size]
            grountruth_smi_list = traingroundtruth_datalist[batch_idx*self.C.batch_size : (batch_idx+1)*self.C.batch_size]



            # return the output
            output,linker_loss ,_= self.model(nodes_linker,edges_linker, nodes_fragment, edges_fragment,is_train=True,fragment_smi_list=fragment_smi_list,grountruth_smi_list=grountruth_smi_list,epoch=self.current_epoch)

           
            
            
            # clear the gradients of all optimized `(torch.Tensor)`s
            self.model.zero_grad()
            self.optimizer.zero_grad()
            # compute the loss
            
           
            batch_loss = loss.graph_generation_loss(
                output=output,
                target_output=target_output,
            )
            linker_loss = torch.mean(linker_loss.float())
            batch_loss = batch_loss+linker_loss

            loss_tensor[batch_idx] = batch_loss

            # backpropagate
            batch_loss.backward()
            self.optimizer.step()

            # update the learning rate
            self.scheduler.step()


        util.write_model_status(
            epoch=self.current_epoch,
            lr=self.optimizer.param_groups[0]["lr"],
            loss=torch.mean(loss_tensor),
        )
        return n_processed_batches