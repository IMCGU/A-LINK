import readMTP, readDFW
import itertools
import committee
import siamese
import noise
import helpers

import numpy as np
import tensorflow as tf

import keras

from keras_vggface import utils
from tensorflow.python.platform import flags
from sklearn.metrics import confusion_matrix
from sets import Set
from tqdm import tqdm
import sys


def init():
	# Set seed for reproducability
	tf.set_random_seed(42)

	# Don't hog GPU
	config = tf.ConfigProto()
	config.gpu_options.allow_growth=True
	sess = tf.Session(config=config)
	keras.backend.set_session(sess)

	# Set low image resolution
	assert(FLAGS.lowRes <= GlobalConstants.normal_res)
	GlobalConstants.low_res = (FLAGS.lowRes, FLAGS.lowRes)


class GlobalConstants:
	image_res = (224, 224)
	feature_res = (2048,)
	normal_res = (150, 150)
	low_res = (32,32)
	# feature_res = (25088,)
	active_count = 0
	sess = keras.backend.get_session()

FLAGS = flags.FLAGS

flags.DEFINE_string('dataDirPrefix', '../MultiPieSplits/split1/train', 'Path to MTP data directory')
flags.DEFINE_string('testDir', '../MultiPieSplits/split1/test', 'Path to MTP test data directory')
flags.DEFINE_string('out_model', 'MTP_models/postALINK', 'Name of model to be saved after finetuning')
flags.DEFINE_string('ensemble_basepath', 'MTP_models/ensemble', 'Prefix for ensemble models')
flags.DEFINE_string('lowres_basemodel', 'MTP_models/lowresModel', 'Name for model trained on low-res faces')
# flags.DEFINE_string('noise', 'gaussian,saltpepper,poisson,perlin,speckle,adversarial', 'Noise components')
flags.DEFINE_string('noise', 'adversarial', 'Noise components')

flags.DEFINE_integer('lowRes', 48, 'Resolution for low-res model (X,X)')
flags.DEFINE_integer('ft_epochs', 3, 'Number of epochs while finetuning model')
flags.DEFINE_integer('batch_size', 16, 'Batch size while sampling from unlabelled data')
flags.DEFINE_integer('lowres_epochs', 10, 'Number of epochs while training lowres-faces model')
flags.DEFINE_integer('highres_epochs', 5, 'Number of epochs while fine-tuning highres-faces model')
flags.DEFINE_integer('batch_send', 32, 'Batch size while finetuning disguised-faces model')
flags.DEFINE_integer('mixture_ratio', 1, 'Ratio of unperturbed:perturbed examples while finetuning network')
flags.DEFINE_integer('alink_bs', 8, 'Batch size to be used while running framework')
flags.DEFINE_integer('num_ensemble_models', 1, 'Number of models to use in ensemble for highres-faces')

flags.DEFINE_float('active_ratio', 1.0, 'Upper cap on ratio of unlabelled examples to be querried for labels')
flags.DEFINE_float('split_ratio', 0.5, 'How much of disguised-face data to use for training M2')
flags.DEFINE_float('disparity_ratio', 0.25, 'What percentage of data to pick to pass on to oracle')
flags.DEFINE_float('eps', 0.1, 'Region around equiboundary for even considering querying the oracle')

flags.DEFINE_boolean('augment', False, 'Augment data while finetuning covariate-based model?')
flags.DEFINE_boolean('refine_models', False, 'Refine previously trained models?')
flags.DEFINE_boolean('blind_strategy', False, 'If yes, pick all where disparity >= 0.5, otherwise pick according to disparity_ratio')


if __name__ == "__main__":
	# Reproducability
	init()

	# Set resolution according to flag
	GlobalConstants.low_res = (FLAGS.lowRes, FLAGS.lowRes)
	print("== Low resolution : %s ==" % str(GlobalConstants.low_res))

	# Define image featurization model
	conversionModel = siamese.RESNET50(GlobalConstants.image_res)

	# Load train images
	X_dig_raw = readMTP.readAllImages(FLAGS.dataDirPrefix)

	# Some sanity checks
	assert(0 <= FLAGS.split_ratio and FLAGS.split_ratio <= 1)
	assert(0 <= FLAGS.disparity_ratio and FLAGS.disparity_ratio <= 1)
	assert(0 <= FLAGS.eps and FLAGS.eps < 0.5)
	print("== Noise that will be used for ALINK: %s ==" % (FLAGS.noise))

	# Set X_dig_post for finetuning second version of model
	if FLAGS.split_ratio > 0:
		(X_dig_pre, X_dig_post) = readDFW.splitDisguiseData(X_dig_raw, pre_ratio=FLAGS.split_ratio)
	elif FLAGS.split_ratio == 1:
		X_dig_pre = X_dig_raw
	else:
		X_dig_post = X_dig_raw

	# Construct ensemble of models
	ensemble = [siamese.SiameseNetwork(GlobalConstants.feature_res, FLAGS.ensemble_basepath + str(i), 1e-1) for i in range(1, FLAGS.num_ensemble_models + 1)]
	
	# Ready low-resolution model
	lowResModel = siamese.SmallRes(GlobalConstants.low_res + (3,), GlobalConstants.feature_res, FLAGS.lowres_basemodel + str(FLAGS.lowRes) , 1e-1)

	# Prepare required noises
	desired_noises = FLAGS.noise.split(',')
	ensembleNoise = [noise.get_relevant_noise(x)(model=lowResModel, sess=GlobalConstants.sess, feature_model=None) for x in desired_noises]

	# Ready committee of models
	bag = committee.Bagging(ensemble, ensembleNoise)

	if not lowResModel.maybeLoadFromMemory():
		print('== Training lowres-faces model ==')
		# Create generators for low-res data
		normGen = readDFW.getNormalGenerator(X_dig_pre, FLAGS.batch_size)
		lowResSiamGen = readMTP.getGenerator(normGen, FLAGS.batch_size, GlobalConstants.low_res)
		lowResModel.customTrainModel(lowResSiamGen, FLAGS.lowres_epochs, FLAGS.batch_size, 0.2, 32000, preprocess=True)
		lowResModel.save()
		exit()
	else:
		lowResModel.maybeLoadFromMemory()
		print('== Loaded lowres-faces model from memory ==')

	normGen = readDFW.getNormalGenerator(X_dig_pre, FLAGS.batch_size)

	# Train/Finetune undisguised model(s), if not already trained
	# for individualModel in ensemble:
	# 	if FLAGS.refine_models:
	# 		individualModel.maybeLoadFromMemory()
	# 		dataGen = readMTP.getGenerator(normGen, FLAGS.batch_size, GlobalConstants.image_res, conversionModel)
	# 		individualModel.customTrainModel(dataGen, FLAGS.highres_epochs, FLAGS.batch_size, 0.2, 32000)
	# 		individualModel.save()
	# 	elif not individualModel.maybeLoadFromMemory():
	# 		print("== Training ensemble model ==")
	# 		dataGen = readMTP.getGenerator(normGen, FLAGS.batch_size, GlobalConstants.image_res, conversionModel)
	# 		individualModel.customTrainModel(dataGen, FLAGS.highres_epochs, FLAGS.batch_size, 0.2, 32000)
	# 		individualModel.save()
	# 		exit()

	# Train lowres-faces model only when batch length crosses threshold
	train_lr_left_x  = np.array([])
	train_lr_right_x = np.array([])
	train_lr_y       = np.array([])
	UN_SIZE          = 0

	# Framework begins
	print("== Framework beginning with a pool of %d ==" % (len(X_dig_post)))
	dataGen = readMTP.getGenerator(normGen, FLAGS.batch_size, GlobalConstants.low_res)
	for ii in range(0, len(X_dig_post), FLAGS.alink_bs):
		print("\nIteration #%d" % ((ii / FLAGS.alink_bs) + 1))
		plain_part = X_dig_post[ii: ii + FLAGS.alink_bs]

		# Create pairs of images
		batch_x, batch_y = readMTP.createMiniBatch(plain_part)

		# batch_y here acts as a pseudo-oracle
		# any reference mde to it is counted as a query to the oracle
		UN_SIZE += len(batch_x[0])

		batch_x_highres  = readMTP.resizeImages(batch_x, GlobalConstants.image_res)
		batch_x_lowres   = readMTP.resizeImages(batch_x, GlobalConstants.low_res)
		
		# Get featurized faces to be passed to committee
		batch_x_features = [conversionModel.process(p) for p in batch_x_highres]

		# Get predictions made by committee
		ensemblePredictions = bag.predict(batch_x_features)
		
		# Get images with added noise
		m1_labels  = keras.utils.to_categorical(np.argmax(ensemblePredictions, axis=1), 2)
		noisy_data = bag.attackModel(batch_x, GlobalConstants.low_res, m1_labels)

		# Pass these to disguised-faces model, get predictions
		disguisedPredictions = [lowResModel.predict([noisy_data[0][jj], noisy_data[1][jj]]) for jj in range(len(ensembleNoise))] 
		misclassifiedIndices = []
		for dp in disguisedPredictions:
			disparities = []
			for j in range(len(dp)):
				c1 = dp[j][0]
				c2 = ensemblePredictions[j][0]
				if FLAGS.blind_strategy:
					if (c1 >= 0.5) != (c2 >= 0.5):
						disparities.append(j)
				else:
					disparities.append(-np.absolute(c1 - c2))
			if not FLAGS.blind_strategy:
				disparities = np.argsort(disparities)[:int(len(disparities) * FLAGS.disparity_ratio)]
			misclassifiedIndices.append(disparities)
		# Pick cases where all noise makes the model flip predictions (according to criteria)
		all_noise_works = Set(misclassifiedIndices[0])
		for j in range(1, len(misclassifiedIndices)):
			all_noise_works = all_noise_works & Set(misclassifiedIndices[j])
		misclassifiedIndices = list(all_noise_works)

		# Query oracle, pick examples for which ensemble was (crudely) right
		queryIndices = []
		for j in misclassifiedIndices:
			# If ensemble's predictions not in grey area:
			ensemble_prediction = ensemblePredictions[j][0]
			if ensemble_prediction <= 0.5 - FLAGS.eps or ensemble_prediction >= 0.5 + FLAGS.eps:
				c1 = ensemble_prediction >= 0.5
				c2 = batch_y[j][0] >= 0.5
				GlobalConstants.active_count += 1
				if c1 == c2:
					queryIndices.append(j)

		# Log active count so far
		print("== Active Count so far : %d ==" % (GlobalConstants.active_count))

		# If nothing matches, proceed to next set of predictions
		if len(queryIndices) == 0:
			print("== Nothing in this set. Skipping batch ==")
			continue

		intermediate = []
		for i in queryIndices:
			intermediate.append(ensemblePredictions[i][0])
		intermediate = np.array(intermediate)

		mp = int(len(intermediate) / float(len(ensembleNoise)))
		# Gather data to be sent to low res model for training
		if train_lr_y.shape[0] > 0:
			train_lr_left_x  = np.concatenate((train_lr_left_x,)  + tuple([noisy_data[0][i][queryIndices[i*mp :(i+1)*mp]] for i in range(len(ensembleNoise))]))
			train_lr_right_x = np.concatenate((train_lr_right_x,) + tuple([noisy_data[1][i][queryIndices[i*mp :(i+1)*mp]] for i in range(len(ensembleNoise))]))
			train_lr_y       = np.concatenate((train_lr_y,)       + tuple([helpers.roundoff(intermediate)[i*mp:(i+1)*mp]  for i in range(len(ensembleNoise))]))
		else:
			train_lr_left_x  = np.concatenate([noisy_data[0][i][queryIndices[i*mp: (i+1)*mp]] for i in range(len(ensembleNoise))])
			train_lr_right_x = np.concatenate([noisy_data[1][i][queryIndices[i*mp: (i+1)*mp]] for i in range(len(ensembleNoise))])
			train_lr_y       = np.concatenate([helpers.roundoff(intermediate)[i*mp:(i+1)*mp]  for i in range(len(ensembleNoise))])

		if train_lr_y.shape[0] >= FLAGS.batch_send:
			# Finetune lowres-faces model with these actively selected data points
			# Also, add unperturbed images to avoid overfitting on noise
			(X_old_left, X_old_right), Y_old = dataGen.next()
			for _ in range(FLAGS.mixture_ratio - 1):
				X_old_temp, Y_old_temp = dataGen.next()
				X_old_left  = np.concatenate((X_old_left,  X_old_temp[0]))
				X_old_right = np.concatenate((X_old_right, X_old_temp[1]))
				Y_old       = np.concatenate((Y_old, Y_old_temp))
			if FLAGS.augment:
				batch_x_aug, batch_y_aug = helpers.augment_data([batch_x_left[queryIndices], batch_x_left[queryIndices]], helpers.roundoff(intermediate), 1)
				train_lr_left_x  = np.concatenate((train_lr_left_x,  batch_x_aug[0], X_old_left))
				train_lr_right_x = np.concatenate((train_lr_right_x, batch_x_aug[1], X_old_right))
				train_lr_y       = np.concatenate((train_lr_y,       batch_y_aug,    Y_old))
			else:
				train_lr_left_x  = np.concatenate((train_lr_left_x,  batch_x_lowres[0][queryIndices], X_old_left))
				train_lr_right_x = np.concatenate((train_lr_right_x, batch_x_lowres[1][queryIndices], X_old_right))
				train_lr_y       = np.concatenate((train_lr_y,       helpers.roundoff(intermediate),    Y_old))

			# Use a lower learning rate for finetuning ?
			lowResModel.finetune([train_lr_left_x, train_lr_right_x], train_lr_y, FLAGS.ft_epochs, 16, 1)
			train_lr_left_x  = np.array([])
			train_lr_right_x = np.array([])
			train_lr_y       = np.array([])

		# Stop algorithm if limit reached/exceeded
		if int(FLAGS.active_ratio * UN_SIZE) <= GlobalConstants.active_count:
			print("== Specified limit reached! Stopping algorithm ==")
			break

	# Print count of images queried so far
	print("== Active Count: %d out of %d ==" % (GlobalConstants.active_count, UN_SIZE))

	# Save retrained model
	lowResModel.save(FLAGS.out_model)

	# Load test images
	X_test = readMTP.readAllImages(FLAGS.testDir, GlobalConstants.low_res)

	# Create gallery images per person
	X_gallery = [x[0] for x in X_test]

	# Calculate top-1 identification accuracy
	total_count, acc = 0, 0
	for i in tqdm(range(len(X_test))):
		for x in X_test[i]:
			left = []
			for gal in X_gallery:
				left.append(x)
			predicted_scores = np.squeeze(lowResModel.predict([np.array(left), np.array(X_gallery)]))
			predicted_id = np.argmax(predicted_scores)
			total_count += 1
			if predicted_id == i:
				acc += 1
	print('Top-1 accuracy : ', acc / float(total_count))
