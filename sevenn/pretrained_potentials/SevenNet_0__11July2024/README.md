## SevenNet-0 (11July2024)
SevenNet-0 (11July2024) is an interatomic potential pre-trained on the [MPTrj(CHGNet) dataset](https://figshare.com/articles/dataset/Materials_Project_Trjectory_MPtrj_Dataset/23713842).

**Warning:** This is NOT the potential referred to in [our paper](https://pubs.acs.org/doi/10.1021/acs.jctc.4c00190). Please check 'SevenNet/pretrained_potentials/SevenNet_0__22May2024' for the referenced there.

SevenNet-0 (11July2024) has the same architecture and number of parameters as SevenNet-0 (22May2024), but the training process and dataset differ.

We found that this model performs better than the previous SevenNet-0 (22May2024). 

It can be directly applied to any system without training and fine-tuned with another dataset if accuracy is unsatisfactory.

- checkpoint_sevennet_0.pth: checkpoint of SevenNet-0 model.
- serial_model/deployed_serial.pt: LAMMPS potential for single GPU runs.
- parallel_model/deployed_parallel_{0-4}.pt: LAMMPS potential for multi-GPU runs.
- fine_tune.yaml: example input.yaml for fine-tuning.
- pre_train.yaml: example input.yaml file used in pre-training.

### Note
You can obtain the same deployed models by running the following command (-p for parallel)
```bash
$ sevenn_get_model {-p} checkpoint_sevennet_0.pth
```
Refer to example_inputs/md_{serial/parallel}_example/ for their usage in LAMMPS.
Both serial and parallel model gives the same results but the parallel model enables multi-GPU MD simulation.
