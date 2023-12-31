# seedの変更による精度の差

import numpy as np
import pickle
import time
import argparse
import sys
import tensorflow as tf
import random
import os
import json

# keras/sklearn libraries
import keras
from keras.preprocessing import sequence
from keras.models import Sequential, Model, load_model
from keras.optimizers import Adam
from keras.layers import Dense, Dropout, Activation, Input, Reshape, BatchNormalization
from keras.layers import (
    Conv1D,
    GlobalAveragePooling1D,
    MaxPooling1D,
    GlobalAveragePooling1D,
    Reshape,
    AveragePooling1D,
    Flatten,
    Concatenate,
)
from keras import backend
from keras.callbacks import TensorBoard, LearningRateScheduler
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import KFold
from sklearn.preprocessing import StandardScaler, RobustScaler, MinMaxScaler
from tensorflow.random import set_seed


parser = argparse.ArgumentParser(description="ML framework")

parser.add_argument(
    "--multi_adsorbate",
    default=0,
    type=int,
    help="train for single adsorbate (0) or multiple (1) (default: 0)",
)
parser.add_argument(
    "--data_dir",
    default="CH_data",
    type=str,
    help="path to file containing DOS and targets (default: CH_data)",
)
parser.add_argument(
    "--run_mode",
    default=0,
    type=int,
    help="run regular (0) or 5-fold CV (1) (default: 0)",
)
parser.add_argument(
    "--split_ratio", default=0.2, type=float, help="train/test ratio (default:0.2)"
)
parser.add_argument(
    "--epochs", default=60, type=int, help="number of total epochs to run (default:60)"
)
parser.add_argument(
    "--batch_size", default=128, type=int, help="batch size (default:32)"
)
parser.add_argument(
    "--channels", default=9, type=int, help="number of channels (default: 9)"
)
parser.add_argument(
    "--seed",
    default=0,
    type=int,
    help="seed for data split(epochs), 0=random (default:0)",
)
parser.add_argument(
    "--save_model",
    default=0,
    type=int,
    help="path to file containing DOS and targets (default: 0)",
)
parser.add_argument(
    "--load_model",
    default=0,
    type=int,
    help="path to file containing DOS and targets (default: 0)",
)
parser.add_argument(
    "--kfold_num",
    default=5,
    type=int,
    help="kfoldの回数。(dfault:5)"
)

args = parser.parse_args(sys.argv[1:])

def reset_random_seed(seed):
    os.environ['PYTHONHASHSEED'] = '0'
    os.environ['TF_DETERMINISTIC_OPS'] = 'true'
    os.environ['TF_CUDNN_DETERMINISTIC'] = 'true'
    tf.random.set_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    session_conf = tf.compat.v1.ConfigProto(intra_op_parallelism_threads=32, inter_op_parallelism_threads=32)
    tf.compat.v1.set_random_seed(seed)
    sess = tf.compat.v1.Session(graph=tf.compat.v1.get_default_graph(), config=session_conf)
    tf.keras.utils.set_random_seed(1)
    tf.config.experimental.enable_op_determinism()


def main():
    start_time = time.time()
    datadir = f"data/{args.data_dir}"
    log = {}

    # load data (replace with your own depending on the data format)
    # Data format for x_surface_dos and x_adsorbate_dos is a numpy array with shape: (A, B, C) where A is number of samples, B is length of DOS file (2000), C is number of channels.
    # Number of channels here is 27 for x_surface_dos which contains 9 orbitals x up to 3 adsorbing surface atoms. E.g. a top site will have the first 9 channels filled and remaining as zeros.
    x_surface_dos, x_adsorbate_dos, y_targets = load_data(
        args.multi_adsorbate, datadir
    )

    if args.seed == 0:
        args.seed = np.random.randint(1, 1e6)

    if args.run_mode == 0:
        mode = "regular"
        run_training(args, x_surface_dos, x_adsorbate_dos, y_targets,log)
    elif args.run_mode == 1:
        mode = "kfold"
        kfold_test(args, x_surface_dos, x_adsorbate_dos, y_targets)
        run_kfold(args, x_surface_dos, x_adsorbate_dos, y_targets,log)
    print("--- %s seconds ---" % (time.time() - start_time))
    print(log)
    # float32型のデータをfloat型に変換
    log = {k: float(v) for k, v in log.items()}
    with(open(f"result/seed_dropout/{args.data_dir}_seed_dropout_{mode}{args.kfold_num}_log.txt", "w")) as f:
        f.write(json.dumps(log))


def load_data(multi_adsorbate, data_dir):
    ###load data containing: (1) dos of surface, (2) adsorption energy(target), (3) dos of adsorbate in gas phase (for multi-adsorbate)
    if args.multi_adsorbate == 0:
        with open(data_dir, "rb") as f:
            surface_dos = pickle.load(f)
            targets = pickle.load(f)
        x_adsorbate_dos = []
    elif args.multi_adsorbate == 1:
        with open(data_dir, "rb") as f:
            surface_dos = pickle.load(f)
            targets = pickle.load(f)
            x_adsorbate_dos = pickle.load(f)
    ###Some data rearranging, depends on if atomic params are to be included as extra features in the DOS series or separately
    ###entries 1700-2200 of the data are set to zero, these are states far above fermi level which seem to cause additional errors, reason being some states are not physically reasonable

    ###First column is energy; not used in current implementation
    surface_dos = surface_dos[:, 0:2000, 1:28]
    ###States far above fermi level can be unphysical and set to zero
    surface_dos[:, 1800:2000, 0:27] = 0
    ###float32 is used for memory concerns
    surface_dos = surface_dos.astype(np.float32)

    if args.multi_adsorbate == 1:
        x_adsorbate_dos = x_adsorbate_dos[:, 0:2000, 1:10]
        x_adsorbate_dos = x_adsorbate_dos.astype(np.float32)

    return surface_dos, x_adsorbate_dos, targets


###Creates the ML model with keras
###This is the overall model where all 3 adsorption sites are fitted at the same time
def create_model(shared_conv, channels, seed, dropout):
    
    set_seed(seed)
    ###Each input represents one out of three possible bonding atoms
    input1 = Input(shape=(2000, channels))
    input2 = Input(shape=(2000, channels))
    input3 = Input(shape=(2000, channels))

    conv1 = shared_conv(input1)
    conv2 = shared_conv(input2)
    conv3 = shared_conv(input3)

    convmerge = Concatenate(axis=-1)([conv1, conv2, conv3])
    convmerge = Flatten()(convmerge)
    convmerge = Dropout(dropout, seed=args.seed)(convmerge)
    convmerge = Dense(200, activation="linear")(convmerge)
    convmerge = Dense(1000, activation="relu")(convmerge)
    convmerge = Dense(1000, activation="relu")(convmerge)

    out = Dense(1, activation="linear")(convmerge)
    # shared_conv.summary()
    model = Model([input1, input2, input3],out)
    return model


###This is the overall model where all 3 adsorption sites are fitted at the same time, and all adsorbates are fitted as well
def create_model_combined(shared_conv, channels):

    ###Each input represents one out of three possible bonding atoms
    input1 = Input(shape=(2000, channels))
    input2 = Input(shape=(2000, channels))
    input3 = Input(shape=(2000, channels))
    input4 = Input(shape=(2000, channels))

    conv1 = shared_conv(input1)
    conv2 = shared_conv(input2)
    conv3 = shared_conv(input3)

    adsorbate_conv = adsorbate_dos_featurizer(channels)
    conv4 = adsorbate_conv(input4)

    convmerge = Concatenate(axis=-1)([conv1, conv2, conv3, conv4])
    convmerge = Flatten()(convmerge)
    convmerge = Dropout(0.2)(convmerge)
    convmerge = Dense(200, activation="linear")(convmerge)
    convmerge = Dense(1000, activation="relu")(convmerge)
    convmerge = Dense(1000, activation="relu")(convmerge)

    out = Dense(1, activation="linear")(convmerge)

    model = Model(input=[input1, input2, input3, input4], output=out)
    return model


###This sub-model is the convolutional network for the DOS
###Uses the same model for each atom input channel
###Input is a 2000 length DOS data series
def dos_featurizer(channels):
    input_dos = Input(shape=(2000, channels))
    x1 = AveragePooling1D(pool_size=4, strides=4, padding="same")(input_dos)
    x2 = AveragePooling1D(pool_size=25, strides=4, padding="same")(input_dos)
    x3 = AveragePooling1D(pool_size=200, strides=4, padding="same")(input_dos)
    x = Concatenate(axis=-1)([x1, x2, x3])
    x = Conv1D(50, 20, activation="relu", padding="same", strides=2)(x)
    x = BatchNormalization()(x)
    x = Conv1D(75, 3, activation="relu", padding="same", strides=2)(x)
    x = AveragePooling1D(pool_size=3, strides=2, padding="same")(x)
    x = Conv1D(100, 3, activation="relu", padding="same", strides=2)(x)
    x = AveragePooling1D(pool_size=3, strides=2, padding="same")(x)
    x = Conv1D(125, 3, activation="relu", padding="same", strides=2)(x)
    x = AveragePooling1D(pool_size=3, strides=2, padding="same")(x)
    x = Conv1D(150, 3, activation="relu", padding="same", strides=1)(x)
    shared_model = Model(input_dos, x)
    return shared_model


###Uses the same model for adsorbate but w/ separate weights
def adsorbate_dos_featurizer(channels):
    input_dos = Input(shape=(2000, channels))
    x1 = AveragePooling1D(pool_size=4, strides=4, padding="same")(input_dos)
    x2 = AveragePooling1D(pool_size=25, strides=4, padding="same")(input_dos)
    x3 = AveragePooling1D(pool_size=200, strides=4, padding="same")(input_dos)
    x = Concatenate(axis=-1)([x1, x2, x3])
    x = Conv1D(50, 20, activation="relu", padding="same", strides=2)(x)
    x = BatchNormalization()(x)
    x = Conv1D(75, 3, activation="relu", padding="same", strides=2)(x)
    x = AveragePooling1D(pool_size=3, strides=2, padding="same")(x)
    x = Conv1D(100, 3, activation="relu", padding="same", strides=2)(x)
    x = AveragePooling1D(pool_size=3, strides=2, padding="same")(x)
    x = Conv1D(125, 3, activation="relu", padding="same", strides=2)(x)
    x = AveragePooling1D(pool_size=3, strides=2, padding="same")(x)
    x = Conv1D(150, 3, activation="relu", padding="same", strides=1)(x)
    shared_model = Model(input_dos, x)
    return shared_model


###Simple learning rate scheduler
def decay_schedule(epoch, lr):
    if epoch == 0:
        lr = 0.001
    elif epoch == 15:
        lr = 0.0005
    elif epoch == 35:
        lr = 0.0001
    elif epoch == 45:
        lr = 0.00005
    elif epoch == 55:
        lr = 0.00001
    return lr

def are_lists_equal(list1, list2):
    return np.array_equal(list1, list2)

# regular training
def run_training(args, x_surface_dos, x_adsorbate_dos, y_targets, log):

    ###Split data into train and test
    if args.multi_adsorbate == 0:
        x_train, x_test, y_train, y_test = train_test_split(
            x_surface_dos, y_targets, test_size=args.split_ratio, random_state=88
        )
    elif args.multi_adsorbate == 1:
        x_train, x_test, y_train, y_test, ads_train, ads_test = train_test_split(
            x_surface_dos,
            y_targets,
            x_adsorbate_dos,
            test_size=args.split_ratio,
            random_state=88,
        )
    ###Scaling data
    scaler = StandardScaler()
    x_train = scaler.fit_transform(x_train.reshape(-1, x_train.shape[2])).reshape(
        x_train.shape
    )
    x_test = scaler.transform(x_test.reshape(-1, x_test.shape[2])).reshape(x_test.shape)

    if args.multi_adsorbate == 1:
        ads_train = scaler.fit_transform(
            ads_train.reshape(-1, ads_train.shape[2])
        ).reshape(ads_train.shape)
        ads_test = scaler.transform(ads_test.reshape(-1, ads_test.shape[2])).reshape(
            ads_test.shape
        )

    ###call and fit model
    shared_conv = dos_featurizer(args.channels)
    lr_scheduler = LearningRateScheduler(decay_schedule, verbose=0)
    tensorboard = TensorBoard(log_dir="logs/{}".format(time.time()), histogram_freq=1)

    ### define seed
    seed_list = [42, 666, 2023, 1, 3]
    
    for count, seed_val in enumerate(seed_list):
        if count == 0:#初回のみepoch0で実行し再現性の確認
            results = []
            for i in range(2):
                reset_random_seed(seed_val)
                if args.multi_adsorbate == 0:
                    if args.load_model == 0:
                        model = create_model(shared_conv, args.channels)
                        model.compile(
                            loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
                        )
                    elif args.load_model == 1:
                        print("Loading model...")
                        model = load_model(f"models/seed_{seed_val}_saved.h5", compile=False)
                        model.compile(
                            loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
                        )
                    model.summary()
                    model.fit(
                        [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27]],
                        y_train,
                        batch_size=args.batch_size,
                        epochs=0,
                        validation_data=(
                            [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27]],
                            y_test,
                        ),
                        callbacks=[tensorboard, lr_scheduler],
                    )
                    train_out = model.predict(
                        [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27]]
                    )
                    train_out = train_out.reshape(len(train_out))
                    test_out = model.predict(
                        [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27]]
                    )
                    test_out = test_out.reshape(len(test_out))
                    result = [train_out, test_out]
                    results.append(result)
                    with open(f"result/seed/{args.data_dir}_initial_value{i}_train.txt", "w") as f:
                        np.savetxt(f, np.stack((y_train, train_out), axis=-1))
                    with open(f"result/seed/{args.data_dir}_initial_value{i}_test.txt", "w") as f:
                        np.savetxt(f, np.stack((y_test, test_out), axis=-1))
                    del model, train_out, test_out
                elif args.multi_adsorbate == 1:
                    model = create_model_combined(shared_conv, args.channels)
                    model.compile(
                        loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
                    )
                    model.summary()
                    model.fit(
                        [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27], ads_train],
                        y_train,
                        batch_size=args.batch_size,
                        epochs=0,
                        validation_data=(
                            [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27], ads_test],
                            y_test,
                        ),
                        callbacks=[tensorboard, lr_scheduler],
                    )
                    train_out = model.predict(
                        [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27], ads_train]
                    )
                    train_out = train_out.reshape(len(train_out))
                    test_out = model.predict(
                        [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27], ads_test]
                    )
                    test_out = test_out.reshape(len(test_out))
                    result = [train_out, test_out]
                    results.append(result)
                    del model, train_out, test_out
            if are_lists_equal(results[0], results[1]):
                print("result is not same")
                sys.exit()
            else:
                print("result is same")
        #再現性が確保されると以下の処理を実行
        reset_random_seed(seed_val)
        if args.multi_adsorbate == 0:
            if args.load_model == 0:
                model = create_model(shared_conv, args.channels)
                model.compile(
                    loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
                )
            elif args.load_model == 1:
                print("Loading model...")
                model = load_model(f"models/seed_{seed_val}_saved.h5", compile=False)
                model.compile(
                    loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
                )
            model.summary()
            model.fit(
                [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27]],
                y_train,
                batch_size=args.batch_size,
                epochs=args.epochs,
                validation_data=(
                    [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27]],
                    y_test,
                ),
                callbacks=[tensorboard, lr_scheduler],
            )
            train_out = model.predict(
                [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27]]
            )
            train_out = train_out.reshape(len(train_out))
            test_out = model.predict(
                [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27]]
            )
            test_out = test_out.reshape(len(test_out))

        elif args.multi_adsorbate == 1:
            model = create_model_combined(shared_conv, args.channels)
            model.compile(
                loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
            )
            model.summary()
            model.fit(
                [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27], ads_train],
                y_train,
                batch_size=args.batch_size,
                epochs=args.epochs,
                validation_data=(
                    [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27], ads_test],
                    y_test,
                ),
                callbacks=[tensorboard, lr_scheduler],
            )
            train_out = model.predict(
                [x_train[:, :, 0:9], x_train[:, :, 9:18], x_train[:, :, 18:27], ads_train]
            )
            train_out = train_out.reshape(len(train_out))
            test_out = model.predict(
                [x_test[:, :, 0:9], x_test[:, :, 9:18], x_test[:, :, 18:27], ads_test]
            )
            test_out = test_out.reshape(len(test_out))

        ###this is just to write the results to a file
        print("seed: ",seed_val)
        print("train MAE: ", mean_absolute_error(y_train, train_out))
        print("train RMSE: ", mean_squared_error(y_train, train_out) ** (0.5))
        print("test MAE: ", mean_absolute_error(y_test, test_out))
        print("test RMSE: ", mean_squared_error(y_test, test_out) ** (0.5))

        log[f"{seed_val}_train_mae"] = mean_absolute_error(y_train, train_out)
        log[f"{seed_val}_train_rmse"] = mean_squared_error(y_train, train_out) ** (0.5)
        log[f"{seed_val}_test_mae"] = mean_absolute_error(y_test, test_out)
        log[f"{seed_val}_test_rmse"] = mean_squared_error(y_test, test_out) ** (0.5)

        #入力データ名を取得
        data_dir = args.data_dir
        
        with open(f"result/seed/{data_dir}_seed{seed_val}_predict_train.txt", "w") as f:
            np.savetxt(f, np.stack((y_train, train_out), axis=-1))
        with open(f"result/seed/{data_dir}_seed{seed_val}_predict_test.txt", "w") as f:
            np.savetxt(f, np.stack((y_test, test_out), axis=-1))

        if args.save_model == 1:
            print("Saving model...")
            model.save(f"models/seed_{seed_val}_saved.h5")


#再現性の確認
def kfold_test(args, x_surface_dos_raw, x_adsorbate_dos, y_targets):
    results = []
    for i in range(2):
        x_surface_dos = x_surface_dos_raw.copy()
        seed = args.seed
        reset_random_seed(seed)
        kfold = KFold(n_splits=5, shuffle=True, random_state=seed)
        splits = list(kfold.split(x_surface_dos, y_targets))
        train, test = splits[0]
        scaler_CV = StandardScaler()
        x_surface_dos[train, :, :] = scaler_CV.fit_transform(
            x_surface_dos[train, :, :].reshape(-1, x_surface_dos[train, :, :].shape[-1])
        ).reshape(x_surface_dos[train, :, :].shape)
        x_surface_dos[test, :, :] = scaler_CV.transform(
            x_surface_dos[test, :, :].reshape(-1, x_surface_dos[test, :, :].shape[-1])
        ).reshape(x_surface_dos[test, :, :].shape)

        if args.multi_adsorbate == 1:
            x_adsorbate_dos[train, :, :] = scaler_CV.fit_transform(
                x_adsorbate_dos[train, :, :].reshape(
                    -1, x_adsorbate_dos[train, :, :].shape[-1]
                )
            ).reshape(x_adsorbate_dos[train, :, :].shape)
            x_adsorbate_dos[test, :, :] = scaler_CV.transform(
                x_adsorbate_dos[test, :, :].reshape(
                    -1, x_adsorbate_dos[test, :, :].shape[-1]
                )
            ).reshape(x_adsorbate_dos[test, :, :].shape)
        
        keras.backend.clear_session()
        shared_conv = dos_featurizer(args.channels)
        lr_scheduler = LearningRateScheduler(decay_schedule, verbose=0)
        if args.multi_adsorbate == 0:
            model_CV = create_model(shared_conv, args.channels, 42, 0.0)
            model_CV.compile(
                loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
            )
            model_CV.fit(
                [
                    x_surface_dos[train, :, 0:9],
                    x_surface_dos[train, :, 9:18],
                    x_surface_dos[train, :, 18:27],
                ],
                y_targets[train],
                batch_size=args.batch_size,
                epochs=0,
                verbose=0,
                callbacks=[lr_scheduler],
            )
            scores = model_CV.evaluate(
                [
                    x_surface_dos[test, :, 0:9],
                    x_surface_dos[test, :, 9:18],
                    x_surface_dos[test, :, 18:27],
                ],
                y_targets[test],
                verbose=0,
            )
            train_out_CV_temp = model_CV.predict(
                [
                    x_surface_dos[test, :, 0:9],
                    x_surface_dos[test, :, 9:18],
                    x_surface_dos[test, :, 18:27],
                ]
            )
            # print(train_out_CV_temp)
            train_out_CV_temp = train_out_CV_temp.reshape(len(train_out_CV_temp))
            results.append(train_out_CV_temp)
            del model_CV, train_out_CV_temp
        elif args.multi_adsorbate == 1:
            model_CV = create_model_combined(shared_conv, args.channels)
            model_CV.compile(
                    loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
            )
            model_CV.fit(
                [
                    x_surface_dos[train, :, 0:9],
                    x_surface_dos[train, :, 9:18],
                    x_surface_dos[train, :, 18:27],
                    x_adsorbate_dos[train, :, :],
                ],
                y_targets[train],
                batch_size=args.batch_size,
                epochs=0,
                verbose=0,
                callbacks=[lr_scheduler],
            )
            scores = model_CV.evaluate(
                [
                    x_surface_dos[test, :, 0:9],
                    x_surface_dos[test, :, 9:18],
                    x_surface_dos[test, :, 18:27],
                    x_adsorbate_dos[test, :, :],
                ],
                y_targets[test],
                verbose=0,
            )
            train_out_CV_temp = model_CV.predict(
                [
                    x_surface_dos[test, :, 0:9],
                    x_surface_dos[test, :, 9:18],
                    x_surface_dos[test, :, 18:27],
                    x_adsorbate_dos[test, :, :],
                ]
            )
            train_out_CV_temp = train_out_CV_temp.reshape(len(train_out_CV_temp))
            results.append(train_out_CV_temp)
            del model_CV, train_out_CV_temp

    if are_lists_equal(results[0], results[1]):
        print("result is same")
    else:
        print("result is not same")
        sys.exit()

# kfold
def run_kfold(args, x_surface_dos_raw, x_adsorbate_dos, y_targets,log):
    seed = args.seed
    cvscores = []
    kfold = KFold(n_splits=5, shuffle=True, random_state=seed)
    ### define seed
    seed_list = []
    for i in range(10):
        seed_list.append(42+i)
    dropout_vals = [0.0, 0.2, 0.4, 0.6, 0.8]
    for dropout in dropout_vals:
        for seed_val in seed_list:
            reset_random_seed(seed_val)
            kfold_count = 0
            for train, test in kfold.split(x_surface_dos_raw, y_targets):
                x_surface_dos = x_surface_dos_raw.copy()
                #実験のため、１回のみ実行
                kfold_count += 1
                if kfold_count > args.kfold_num:
                    break

                scaler_CV = StandardScaler()
                x_surface_dos[train, :, :] = scaler_CV.fit_transform(
                    x_surface_dos[train, :, :].reshape(-1, x_surface_dos[train, :, :].shape[-1])
                ).reshape(x_surface_dos[train, :, :].shape)
                x_surface_dos[test, :, :] = scaler_CV.transform(
                    x_surface_dos[test, :, :].reshape(-1, x_surface_dos[test, :, :].shape[-1])
                ).reshape(x_surface_dos[test, :, :].shape)
                if args.multi_adsorbate == 1:
                    x_adsorbate_dos[train, :, :] = scaler_CV.fit_transform(
                        x_adsorbate_dos[train, :, :].reshape(
                            -1, x_adsorbate_dos[train, :, :].shape[-1]
                        )
                    ).reshape(x_adsorbate_dos[train, :, :].shape)
                    x_adsorbate_dos[test, :, :] = scaler_CV.transform(
                        x_adsorbate_dos[test, :, :].reshape(
                            -1, x_adsorbate_dos[test, :, :].shape[-1]
                        )
                    ).reshape(x_adsorbate_dos[test, :, :].shape)

                keras.backend.clear_session()
                shared_conv = dos_featurizer(args.channels)
                lr_scheduler = LearningRateScheduler(decay_schedule, verbose=0)
                if args.multi_adsorbate == 0:
                    model_CV = create_model(shared_conv, args.channels, seed_val, dropout)
                    model_CV.compile(
                        loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
                    )
                    model_CV.fit(
                        [
                            x_surface_dos[train, :, 0:9],
                            x_surface_dos[train, :, 9:18],
                            x_surface_dos[train, :, 18:27],
                        ],
                        y_targets[train],
                        batch_size=args.batch_size,
                        epochs=args.epochs,
                        verbose=0,
                        callbacks=[lr_scheduler],
                    )
                    scores = model_CV.evaluate(
                        [
                            x_surface_dos[test, :, 0:9],
                            x_surface_dos[test, :, 9:18],
                            x_surface_dos[test, :, 18:27],
                        ],
                        y_targets[test],
                        verbose=0,
                    )
                    train_out_CV_temp = model_CV.predict(
                        [
                            x_surface_dos[test, :, 0:9],
                            x_surface_dos[test, :, 9:18],
                            x_surface_dos[test, :, 18:27],
                        ]
                    )
                    train_out_CV_temp = train_out_CV_temp.reshape(len(train_out_CV_temp))
                elif args.multi_adsorbate == 1:
                    model_CV = create_model_combined(shared_conv, args.channels)
                    model_CV.compile(
                        loss="logcosh", optimizer=Adam(0.001), metrics=["mean_absolute_error"]
                    )
                    model_CV.fit(
                        [
                            x_surface_dos[train, :, 0:9],
                            x_surface_dos[train, :, 9:18],
                            x_surface_dos[train, :, 18:27],
                            x_adsorbate_dos[train, :, :],
                        ],
                        y_targets[train],
                        batch_size=args.batch_size,
                        epochs=args.epochs,
                        verbose=0,
                        callbacks=[lr_scheduler],
                    )
                    scores = model_CV.evaluate(
                        [
                            x_surface_dos[test, :, 0:9],
                            x_surface_dos[test, :, 9:18],
                            x_surface_dos[test, :, 18:27],
                            x_adsorbate_dos[test, :, :],
                        ],
                        y_targets[test],
                        verbose=0,
                    )
                    train_out_CV_temp = model_CV.predict(
                        [
                            x_surface_dos[test, :, 0:9],
                            x_surface_dos[test, :, 9:18],
                            x_surface_dos[test, :, 18:27],
                            x_adsorbate_dos[test, :, :],
                        ]
                    )
                    train_out_CV_temp = train_out_CV_temp.reshape(len(train_out_CV_temp))
                print((model_CV.metrics_names[1], scores[1]))
                cvscores.append(scores[1])
                try:
                    train_out_CV = np.append(train_out_CV, train_out_CV_temp)
                    test_y_CV = np.append(test_y_CV, y_targets[test])
                    test_index = np.append(test_index, test)
                except:
                    train_out_CV = train_out_CV_temp
                    test_y_CV = y_targets[test]
                    test_index = test
            print((np.mean(cvscores), np.std(cvscores)))
            print(len(test_y_CV))
            print(len(train_out_CV))
            print(f"seed:{seed_val} CV MAE: ", mean_absolute_error(test_y_CV, train_out_CV))
            print(f"seed:{seed_val} CV RMSE: ", mean_squared_error(test_y_CV, train_out_CV) ** (0.5))
            log[f"{seed_val}_mae"] = mean_absolute_error(test_y_CV, train_out_CV)
            log[f"{seed_val}_rmse"] = mean_squared_error(test_y_CV, train_out_CV) ** (0.5)
            with open(f"result/seed_dropout/{dropout}/{args.data_dir}_CV{args.kfold_num}_seed{seed_val}.txt", "w") as f:
                np.savetxt(f, np.stack((test_y_CV, train_out_CV), axis=-1))
            del model_CV, train_out_CV, test_y_CV, test_index, scores, train_out_CV_temp

if __name__ == "__main__":
    main()
