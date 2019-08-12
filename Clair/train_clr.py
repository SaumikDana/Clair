import sys
import os
import time
import argparse
import logging
import random

import numpy as np
from threading import Thread

import param
import utils
import clair_model as cv
import evaluate

logging.basicConfig(format='%(message)s', level=logging.INFO)

def shuffle_first_n_items(array, n):
    if len(array) <= n:
        np.random.shuffle(array)
        return array
    # pylint: disable=unbalanced-tuple-unpacking
    a1, a2 = np.split(array, [n])
    np.random.shuffle(a1)
    return np.append(a1, a2)


def new_mini_batch(data_index, validation_data_start_index, dataset_info, tensor_block_index_list):
    dataset_size = dataset_info["dataset_size"]
    x_array_compressed = dataset_info["x_array_compressed"]
    y_array_compressed = dataset_info["y_array_compressed"]
    training_batch_size = param.trainBatchSize
    validation_batch_size = param.predictBatchSize

    if data_index >= dataset_size:
        return None, None, 0

    # calculate new batch size according to dataset index
    # train: 0 - validation_data_start_index - 1, validation: validation_data_start_index - dataset_size
    if (
        data_index < validation_data_start_index and
        (validation_data_start_index - data_index) < training_batch_size
    ):
        batch_size = validation_data_start_index - data_index
    elif data_index < validation_data_start_index:
        batch_size = training_batch_size
    elif data_index >= validation_data_start_index and (data_index % validation_batch_size) != 0:
        batch_size = validation_batch_size - (data_index % validation_batch_size)
    elif data_index >= validation_data_start_index:
        batch_size = validation_batch_size

    # extract features(x) and labels(y) for current batch
    x_batch, x_num, x_end_flag = utils.decompress_array_with_order(
        x_array_compressed, data_index, batch_size, dataset_size, tensor_block_index_list)
    y_batch, y_num, y_end_flag = utils.decompress_array_with_order(
        y_array_compressed, data_index, batch_size, dataset_size, tensor_block_index_list)
    if x_num != y_num or x_end_flag != y_end_flag:
        sys.exit("Inconsistency between decompressed arrays: %d/%d" % (x_num, y_num))

    return x_batch, y_batch, x_num


def train_model(m, training_config):
    learning_rate = training_config["learning_rate"]
    max_learning_rate=param.clr_max_lr
    l2_regularization_lambda = training_config["l2_regularization_lambda"]
    output_file_path_prefix = training_config["output_file_path_prefix"]
    summary_writer = training_config["summary_writer"]
    model_initalization_file_path = training_config["model_initalization_file_path"]

    dataset_info = training_config["dataset_info"]
    dataset_size = dataset_info["dataset_size"]

    training_losses = []
    validation_losses = []

    if model_initalization_file_path != None:
        m.restore_parameters(os.path.abspath(model_initalization_file_path))

    logging.info("[INFO] Start training...")
    logging.info("[INFO] Learning rate: %.2e" % m.set_learning_rate(learning_rate))
    logging.info("[INFO] L2 regularization lambda: %.2e" % m.set_l2_regularization_lambda(l2_regularization_lambda))

    tensor_block_index_list = np.arange(int(np.ceil(float(dataset_size) / param.bloscBlockSize)), dtype=int)

    # Model Constants
    training_start_time = time.time()
    no_of_training_examples = int(dataset_size*param.trainingDatasetPercentage)
    validation_data_start_index = no_of_training_examples + 1
    no_of_validation_examples = dataset_size - validation_data_start_index
    validation_start_block = int(validation_data_start_index / param.bloscBlockSize) - 1
    total_numbers_of_iterations = np.ceil(no_of_training_examples / param.trainBatchSize+1)+np.ceil(no_of_validation_examples/param.predictBatchSize+1)
    step_size = param.stepsizeConstant * total_numbers_of_iterations

    # Initialize variables
    epoch_count = 1
    if model_initalization_file_path != None:
        epoch_count = int(model_initalization_file_path[-param.parameterOutputPlaceHolder:])+1

    epoch_start_time = time.time()
    training_loss_sum = 0
    validation_loss_sum = 0
    data_index = 0
    x_batch = None
    y_batch = None
    global_step=0

    base_change_loss_sum = 0
    genotype_loss_sum = 0
    indel_length_loss_sum_1 = 0
    indel_length_loss_sum_2 = 0
    l2_loss_sum = 0

    while epoch_count <= param.maxEpoch:
        is_training = data_index < validation_data_start_index
        is_validation = data_index >= validation_data_start_index
        is_with_batch_data = x_batch is not None and y_batch is not None

        # threads for either train or validation
        thread_pool = []
        if is_with_batch_data and is_training:
            thread_pool.append(Thread(target=m.train, args=(x_batch, y_batch, True)))
        elif is_with_batch_data and is_validation:
            thread_pool.append(Thread(target=m.get_loss, args=(x_batch, y_batch, True)))
        for t in thread_pool:
            t.start()


        next_x_batch, next_y_batch, batch_size= new_mini_batch(
            data_index=data_index,
            validation_data_start_index=validation_data_start_index,
            dataset_info=dataset_info,
            tensor_block_index_list=tensor_block_index_list,
        )


        # wait until loaded next mini batch & finished training/validation with current mini batch
        for t in thread_pool:
            t.join()


        # add training loss or validation loss
        if is_with_batch_data and is_training:
            training_loss_sum += m.trainLossRTVal
            if summary_writer != None:
                summary = m.trainSummaryRTVal
                summary_writer.add_summary(summary, epoch_count)
        elif is_with_batch_data and is_validation:
            validation_loss_sum += m.getLossLossRTVal
            base_change_loss_sum += m.base_change_loss
            genotype_loss_sum += m.genotype_loss
            indel_length_loss_sum_1 += m.indel_length_loss_1
            indel_length_loss_sum_2 += m.indel_length_loss_2
            l2_loss_sum += m.l2_loss

        data_index += batch_size

        # if not go through whole dataset yet (have next x_batch and y_batch data), continue the process
        if next_x_batch is not None and next_y_batch is not None:
            x_batch = next_x_batch
            y_batch = next_y_batch
            learning_rate,global_step,max_learning_rate=m.clr(global_step,step_size,max_learning_rate,args.clr_mode)
            continue

        logging.info(
            " ".join([str(epoch_count), "Training loss:", str(training_loss_sum/no_of_training_examples)])
        )
        logging.info(
            "\t".join([
                "{} Validation loss (Total/Base/Genotype/Indel_1_2):".format(epoch_count),
                str(validation_loss_sum/no_of_validation_examples),
                str(base_change_loss_sum/no_of_validation_examples),
                str(genotype_loss_sum/no_of_validation_examples),
                str(indel_length_loss_sum_1/no_of_validation_examples),
                str(indel_length_loss_sum_2/no_of_validation_examples)
            ])
        )

        logging.info("[INFO] Epoch time elapsed: %.2f s" % (time.time() - epoch_start_time))
        training_losses.append((training_loss_sum, epoch_count))
        validation_losses.append((validation_loss_sum, epoch_count))

        # Output the model
        if output_file_path_prefix != None:
            parameter_output_path = "%s-%%0%dd" % (output_file_path_prefix, param.parameterOutputPlaceHolder)
            m.save_parameters(os.path.abspath(parameter_output_path % epoch_count))

        # variables update per epoch
        epoch_count += 1

        epoch_start_time = time.time()
        training_loss_sum = 0
        validation_loss_sum = 0
        data_index = 0
        x_batch = None
        y_batch = None

        base_change_loss_sum = 0
        genotype_loss_sum = 0
        indel_length_loss_sum_1 = 0
        indel_length_loss_sum_2 = 0
        l2_loss_sum = 0


        # shuffle data on each epoch
        tensor_block_index_list = shuffle_first_n_items(tensor_block_index_list, validation_start_block)
        logging.info("[INFO] Shuffled: " + ' '.join(
            [str(x) for x in np.append(tensor_block_index_list[:5], tensor_block_index_list[-5:])]
        ))

    logging.info("[INFO] Training time elapsed: %.2f s" % (time.time() - training_start_time))
    return training_losses, validation_losses


if __name__ == "__main__":

    random.seed(param.RANDOM_SEED)
    np.random.seed(param.RANDOM_SEED)

    parser = argparse.ArgumentParser(description="Train Clair")

    #clr mode
    parser.add_argument('--clr_mode', type=str, default="tri", help="clr modes: tri, tri2, exp")

    # binary file path
    parser.add_argument('--bin_fn', type=str, default=None,
                        help="Binary tensor input generated by tensor2Bin.py, tensor_fn, var_fn and bed_fn will be ignored")

    # tensor file path
    parser.add_argument('--tensor_fn', type=str, default="vartensors", help="Tensor input")

    # variant file path
    parser.add_argument('--var_fn', type=str, default="truthvars", help="Truth variants list input")

    # bed file path
    parser.add_argument('--bed_fn', type=str, default=None,
                        help="High confident genome regions input in the BED format")

    # checkpoint file path
    parser.add_argument('--chkpnt_fn', type=str, default=None,
                        help="Input a checkpoint for testing or continue training")

    # learning rate, with default value stated in param
    parser.add_argument('--learning_rate', type=float, default=param.initialLearningRate,
                        help="Set the initial learning rate, default: %(default)s")

    # l2 regularization
    parser.add_argument('--lambd', type=float, default=param.l2RegularizationLambda,
                        help="Set the l2 regularization lambda, default: %(default)s")

    # output checkpint file path prefix
    parser.add_argument('--ochk_prefix', type=str, default=None,
                        help="Prefix for checkpoint outputs at each learning rate change, REQUIRED")

    parser.add_argument('--olog_dir', type=str, default=None,
                        help="Directory for tensorboard log outputs, optional")

    args = parser.parse_args()

    if len(sys.argv[1:]) == 0:
        parser.print_help()
        sys.exit(1)

    # initialize
    logging.info("[INFO] Initializing")
    utils.setup_environment()
    m = cv.Clair(optimizer="SGDM")
    m.init()

    dataset_info = utils.dataset_info_from(
        binary_file_path=args.bin_fn,
        tensor_file_path=args.tensor_fn,
        variant_file_path=args.var_fn,
        bed_file_path=args.bed_fn
    )
    training_config = dict(
        dataset_info=dataset_info,
        learning_rate=args.learning_rate,
        l2_regularization_lambda=args.lambd,
        output_file_path_prefix=args.ochk_prefix,
        model_initalization_file_path=args.chkpnt_fn,
        summary_writer=m.get_summary_file_writer(args.olog_dir) if args.olog_dir != None else None,
    )

    _training_losses, validation_losses = train_model(m, training_config)

    # show the parameter set with the smallest validation loss
    validation_losses.sort()
    best_validation_epoch = validation_losses[0][1]
    logging.info("[INFO] Best validation loss at epoch: %d" % best_validation_epoch)

    # load best validation model and evaluate it
    model_file_path = "%s-%%0%dd" % (training_config["output_file_path_prefix"], param.parameterOutputPlaceHolder)
    best_validation_model_file_path = model_file_path % best_validation_epoch
    m.restore_parameters(os.path.abspath(best_validation_model_file_path))
    evaluate.evaluate_model(m, dataset_info)