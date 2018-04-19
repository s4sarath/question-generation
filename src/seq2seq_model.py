# https://arxiv.org/pdf/1705.02012.pdf

import numpy as np
import tensorflow as tf

from tensorflow.python.layers import base
from tensorflow.python.framework import tensor_shape

import helpers.preprocessing as preprocessing


from squad_model import SQuADModel
from helpers.loader import OOV, PAD, EOS, SOS, load_glove

from copy_mechanism import copy_attention_wrapper, copy_layer

import helpers.ops as ops
from helpers.misc_utils import debug_shape, debug_tensor

FLAGS = tf.app.flags.FLAGS

max_copy_size = 818 # 815 plus start, end, pad for good measure..

def ids_to_string(rev_vocab):
    def _ids_to_string(ids, context):
        row_str=[]
        for i,row in enumerate(ids):

            context_tokens = [w.decode() for w in context[i].tolist()]
            out_str = []
            for j in row:
                if j< len(rev_vocab):
                    out_str.append(rev_vocab[j])
                else:
                    out_str.append(context_tokens[j-len(rev_vocab)])
            row_str.append(out_str)
        # return np.asarray(row_str)
        return [row_str]
    return _ids_to_string

def id_tensor_to_string(ids, rev_vocab, context):

    return tf.py_func(ids_to_string(rev_vocab), [ids, context], tf.string)


class Seq2SeqModel(SQuADModel):
    def __init__(self, vocab, batch_size, training_mode=False):
        self.embedding_size = tf.app.flags.FLAGS.embedding_size
        self.context_encoder_units = tf.app.flags.FLAGS.context_encoder_units
        self.answer_encoder_units = tf.app.flags.FLAGS.answer_encoder_units
        self.decoder_units = tf.app.flags.FLAGS.decoder_units
        self.training_mode = training_mode
        super().__init__(vocab, batch_size)

    def build_model(self):

        self.build_data_pipeline(self.batch_size)

        curr_batch_size = tf.shape(self.answer_ids)[0]

        # self.W = tf.get_variable('testvar', [len(self.vocab), len(self.vocab)], initializer=tf.orthogonal_initializer)
        #
        # a_oh = tf.one_hot(tf.mod(self.answer_ids, len(self.vocab)), depth=len(self.vocab))
        # s = tf.shape(a_oh)
        # x = tf.reshape(a_oh, [-1, len(self.vocab)])
        # self.answer_hat = tf.reshape(tf.matmul(x, self.W), s)


        # build teacher output - coerce to vocab and pad with SOS/EOS
        # also build output for loss - one hot over vocab+context
        self.question_onehot = tf.one_hot(self.question_ids, depth=tf.tile([len(self.vocab)+max_copy_size], [curr_batch_size])+self.context_length)
        self.question_coerced = tf.where(tf.greater_equal(self.question_ids, len(self.vocab)), tf.tile(tf.constant([[self.vocab[OOV]]]), tf.shape(self.question_ids)), self.question_ids)
        self.question_teach = tf.concat([tf.tile(tf.constant(self.vocab[SOS], shape=[1, 1]), [curr_batch_size,1]), self.question_ids[:,:-1]], axis=1)
        self.question_teach_oh = tf.one_hot(self.question_teach, depth=len(self.vocab)+max_copy_size)
        # Embed c,q,a
        self.embeddings = tf.get_variable('word_embeddings', [len(self.vocab), self.embedding_size], initializer=tf.orthogonal_initializer)

        # use glove if possible
        self.glove_init_ops =[]
        glove_embeddings = load_glove(FLAGS.data_path, d=FLAGS.embedding_size)
        for word,id in self.vocab.items():
            if word in glove_embeddings.keys():
                self.glove_init_ops.append(tf.assign(self.embeddings[id,:], glove_embeddings[word]))

        # First, coerce them to the shortlist vocab. Then embed
        self.context_coerced = tf.where(tf.greater_equal(self.context_ids, len(self.vocab)), tf.tile(tf.constant([[self.vocab[OOV]]]), tf.shape(self.context_ids)), self.context_ids)
        self.context_embedded = tf.nn.embedding_lookup(self.embeddings, self.context_coerced)

        self.question_teach_embedded = tf.nn.embedding_lookup(self.embeddings, self.question_teach)
        self.question_embedded = tf.nn.embedding_lookup(self.embeddings, self.question_coerced)

        self.answer_coerced = tf.where(tf.greater_equal(self.answer_ids, len(self.vocab)), tf.tile(tf.constant([[self.vocab[OOV]]]), tf.shape(self.answer_ids)), self.answer_ids)
        self.answer_embedded = tf.nn.embedding_lookup(self.embeddings, self.answer_coerced) # batch x seq x embed

        # Is context token in answer?
        max_context_len = tf.reduce_max(self.context_length)
        context_ix = tf.tile(tf.expand_dims(tf.range(max_context_len),axis=0), [curr_batch_size,1])
        gt_start = tf.greater_equal(context_ix, tf.tile(tf.expand_dims(self.answer_locs[:,0],axis=1), [1, max_context_len]))
        lt_end = tf.less(context_ix, tf.tile(tf.expand_dims(self.answer_locs[:,0]+self.answer_length,axis=1), [1, max_context_len]))
        in_answer_feature = tf.expand_dims(tf.cast(tf.logical_and(gt_start, lt_end), tf.float32),axis=2)

        # augment embedding
        self.context_embedded = tf.concat([self.context_embedded, in_answer_feature], axis=2)

        # Build encoder for context
        # Build RNN cell for encoder
        with tf.variable_scope('context_encoder'):
            context_encoder_cell_fwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])
            context_encoder_cell_bwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])

            # Unroll encoder RNN
            context_encoder_output_parts, context_encoder_state = tf.nn.bidirectional_dynamic_rnn(
                context_encoder_cell_fwd, context_encoder_cell_bwd, self.context_embedded,
                sequence_length=self.context_length, dtype=tf.float32)
            self.context_encoder_output = tf.concat([context_encoder_output_parts[0], context_encoder_output_parts[1]], axis=2) # batch x seq x 2*units


        # Build encoder for mean(encoder(context)) + answer
        # Build RNN cell for encoder
        with tf.variable_scope('a_encoder'):
            # To build the "extractive condition encoding" input, take embeddings of answer words concated with encoded context at that position

            # This is super involved! Even though we have the right indices we have to do a LOT of massaging to get them in the right shape
            seq_length = tf.reduce_max(self.answer_length)
            # self.indices = tf.concat([[tf.range(self.answer_pos[i], self.answer_pos[i]+tf.reduce_max(self.answer_length)) for i in range(self.batch_size)]], axis=1)
            self.indices = self.answer_locs
            # cap the indices to be valid
            self.indices = tf.minimum(self.indices, tf.tile(tf.expand_dims(self.context_length-1,axis=1),[1,tf.reduce_max(self.answer_length)]))

            batch_ix = tf.expand_dims(tf.transpose(tf.tile(tf.expand_dims(tf.range(curr_batch_size),axis=0),[seq_length,1]),[1,0]),axis=2)
            full_ix = tf.concat([batch_ix,tf.expand_dims(self.indices,axis=-1)], axis=2)
            self.context_condition_encoding = tf.gather_nd(self.context_encoder_output, full_ix)


            self.full_condition_encoding = tf.concat([self.context_condition_encoding, self.answer_embedded], axis=2)

            a_encoder_cell_fwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])
            a_encoder_cell_bwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])

            # Unroll encoder RNN
            a_encoder_output_parts, a_encoder_state_parts = tf.nn.bidirectional_dynamic_rnn(
                a_encoder_cell_fwd, a_encoder_cell_bwd, self.full_condition_encoding,
                sequence_length=self.answer_length, dtype=tf.float32)

            self.a_encoder_final_state = tf.concat([a_encoder_state_parts[0][0].c, a_encoder_state_parts[1][0].c], axis=1) # batch x 2*a_encoder_units

        # concat direction outputs again

        # build init state
        with tf.variable_scope('decoder_initial_state'):
            L = tf.get_variable('decoder_L', [self.context_encoder_units*2, self.context_encoder_units*2], initializer=tf.orthogonal_initializer(), dtype=tf.float32)
            W0 = tf.get_variable('decoder_W0', [self.context_encoder_units*2, self.decoder_units], initializer=tf.orthogonal_initializer(), dtype=tf.float32)
            b0 = tf.get_variable('decoder_b0', [self.decoder_units], initializer=tf.zeros_initializer(), dtype=tf.float32)

            if False:
                self.context_encoding = self.a_encoder_final_state # this would be the maluuba model
            else:
                self.context_encoding = tf.reduce_mean(self.context_condition_encoding, axis=1) # this is the baseline model

            r = tf.reduce_sum(self.context_encoder_output, axis=1)/tf.tile(tf.expand_dims(tf.cast(self.context_length,tf.float32),axis=1),[1,self.context_encoder_units*2]) + tf.matmul(self.context_encoding,L)
            self.s0 = tf.nn.tanh(tf.matmul(r,W0) + b0)

        # decode
        # TODO: for Maluuba model, decoder inputs are concat of context and answer encoding
        with tf.variable_scope('decoder'):

            if not self.training_mode:
                memory = tf.contrib.seq2seq.tile_batch( self.context_encoder_output, multiplier=FLAGS.beam_width )
                memory_sequence_length = tf.contrib.seq2seq.tile_batch( self.context_length, multiplier=FLAGS.beam_width)
                s0_tiled = tf.contrib.seq2seq.tile_batch( self.s0, multiplier=FLAGS.beam_width)
                init_state = tf.contrib.rnn.LSTMStateTuple(s0_tiled, tf.contrib.seq2seq.tile_batch(tf.zeros([curr_batch_size, self.decoder_units]), multiplier=FLAGS.beam_width))
                # init_state = tf.contrib.rnn.LSTMStateTuple(self.s0, tf.zeros([curr_batch_size, self.decoder_units]))
                # init_state = tf.contrib.seq2seq.tile_batch( init_state, multiplier=FLAGS.beam_width)
            else:
                memory = self.context_encoder_output
                memory_sequence_length = self.context_length
                init_state = tf.contrib.rnn.LSTMStateTuple(self.s0, tf.zeros([curr_batch_size, self.decoder_units]))



            attention_mechanism = copy_attention_wrapper.BahdanauAttention(
                            num_units=self.decoder_units, memory=memory,
                            memory_sequence_length=memory_sequence_length)

            # copy_mechanism = copy_attention_wrapper.BahdanauAttention(
            #                 num_units=self.decoder_units, memory=memory,
            #                 memory_sequence_length=memory_sequence_length)

            decoder_cell = tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.decoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.)))



            # decoder_cell = tf.contrib.seq2seq.AttentionWrapper(decoder_cell,
            #                                                     attention_mechanism,
            #                                                     attention_layer_size=self.decoder_units / 2,
            #                                                     alignment_history=True)

            decoder_cell = copy_attention_wrapper.CopyAttentionWrapper(decoder_cell,
                                                                attention_mechanism,
                                                                attention_layer_size=self.decoder_units / 2,
                                                                alignment_history=False,
                                                                copy_mechanism=attention_mechanism,
                                                                output_attention=True,
                                                                initial_cell_state=init_state)

            init_state = decoder_cell.zero_state(curr_batch_size*(FLAGS.beam_width if not self.training_mode else 1), tf.float32).clone(cell_state=init_state)

            if self.training_mode:
                # Helper - training
                helper = tf.contrib.seq2seq.TrainingHelper(
                    self.question_teach_oh, self.question_length)
                    # decoder_emb_inp, length(decoder_emb_inp)+1)



                # Decoder - training
                decoder = tf.contrib.seq2seq.BasicDecoder(
                    decoder_cell, helper,
                    initial_state=init_state,
                    # initial_state=encoder_state
                    # TODO: hardcoded max_copy_size is longest context in SQuAD - this will need changing for a new dataset!!!
                    output_layer=copy_layer.CopyLayer(FLAGS.embedding_size, max_copy_size,
                                                    source_provider=lambda: self.context_ids,
                                                    condition_encoding=lambda: self.context_encoding,
                                                    vocab_size=len(self.vocab))
                    )

                # Unroll the decoder
                outputs, decoder_states,out_lens = tf.contrib.seq2seq.dynamic_decode(decoder,impute_finished=True, maximum_iterations=tf.reduce_max(self.question_length))

                # projection_layer = tf.layers.Dense(
                #     len(self.vocab), use_bias=False)
                # logits = projection_layer(outputs.rnn_output)
                #
                #
                #
                # self.attention = tf.transpose(decoder_states.alignment_history.stack(),[1,0,2]) # batch x seq x attn

                logits=outputs.rnn_output
                # print(out_lens)
                # print(outputs)
                # print(logits)
                # exit()
            else:
                start_tokens = tf.tile(tf.constant([self.vocab[SOS]], dtype=tf.int32), [ curr_batch_size  ] )
                end_token = self.vocab[EOS]

                projection_layer = copy_layer.CopyLayer(FLAGS.embedding_size, max_copy_size,
                                                source_provider=lambda: self.context_ids,
                                                condition_encoding=lambda: self.context_encoding,
                                                vocab_size=len(self.vocab))

                # init_state = tf.contrib.seq2seq.tile_batch( init_state, multiplier=FLAGS.beam_width )
                # init_state = decoder_cell.zero_state(curr_batch_size * FLAGS.beam_width, tf.float32).clone(cell_state=init_state)
                # init_state = decoder_cell.zero_state(curr_batch_size, tf.float32).clone(cell_state=init_state)



                my_decoder = tf.contrib.seq2seq.BeamSearchDecoder( cell = decoder_cell,
                                                                   embedding = tf.eye(len(self.vocab) + max_copy_size),
                                                                   start_tokens = start_tokens,
                                                                   end_token = end_token,
                                                                   initial_state = init_state,
                                                                   beam_width = FLAGS.beam_width,
                                                                   output_layer = projection_layer )

                # helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
                #       embedding=tf.eye(len(self.vocab) + max_copy_size),
                #       start_tokens=tf.tile(tf.constant([self.vocab[SOS]], dtype=tf.int32), [ curr_batch_size ] ),
                #       end_token=end_token)
                # my_decoder = tf.contrib.seq2seq.BasicDecoder( cell = decoder_cell,
                #                                                 helper=helper,
                #                                                   initial_state = init_state,
                #                                                   output_layer = projection_layer )

                outputs, decoder_states,out_lens = tf.contrib.seq2seq.dynamic_decode(  my_decoder,
                                                                        impute_finished=False,
                                                                       maximum_iterations=32 )

                # logits = outputs.rnn_output

                logits = tf.one_hot(outputs.predicted_ids[:,:,0], depth=len(self.vocab)+max_copy_size)


        # calc switch prob
        # with tf.variable_scope('switch'):
        #     # switch takes st, vt and yt−1 as inputs
        #     # vt = concat(weighted context encoding at t; condition encoding)
        #     # st = hidden state at t
        #     # y_t-1 is previous generated token
        #     context = tf.matmul( self.attention, self.context_embedded)
        #     ha_tiled = tf.tile(tf.expand_dims(context_encoding,axis=1),[1,tf.reduce_max(self.question_length),1])
        #     vt = tf.concat([context, ha_tiled], axis=2)
        #     # NOTE: outputs.rnn_output is y_t-1, should be prev state
        #     switch_input = tf.concat([vt, outputs.rnn_output, self.question_teach_embedded],axis=2)
        #     switch_h1 = tf.layers.dense(switch_input, 64, activation=tf.nn.tanh, kernel_initializer=tf.initializers.orthogonal())
        #     switch_h2 = tf.layers.dense(switch_h1, 64, activation=tf.nn.tanh, kernel_initializer=tf.initializers.orthogonal())
        #     self.switch = tf.layers.dense(switch_h2, 1, activation=tf.sigmoid, kernel_initializer=tf.initializers.orthogonal())
        #
        # # build overall prediction prob vector
        # self.q_hat_shortlist = tf.nn.softmax(logits,dim=2) #NOTE kwarg dim is deprecated in favour of axis, but blaze == 1.4
        #
        # self.q_hat = tf.concat([(1-self.switch)*self.q_hat_shortlist,self.switch*self.attention], axis=2)
        self.q_hat = tf.nn.softmax(logits, dim=2)



        # TODO: include answer-suppression loss and variety loss terms
        with tf.variable_scope('train_loss'):
            self.target_weights = tf.sequence_mask(
                        self.question_length, tf.reduce_max(self.question_length), dtype=tf.float32)
            logits = ops.safe_log(self.q_hat)

            self.crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=self.question_ids, logits=logits)
            self.xe_loss = tf.reduce_mean(tf.reduce_sum(self.crossent * self.target_weights,axis=1),axis=0)

            # TODO: Check these should be included in baseline?
            # get sum of all probabilities for words that are also in answer
            answer_oh = tf.one_hot(self.answer_ids, depth=len(self.vocab) +max_copy_size)
            answer_mask = tf.tile(tf.reduce_sum(answer_oh, axis=1,keep_dims=True), [1,tf.reduce_max(self.question_length),1])
            self.suppression_loss = tf.reduce_sum(answer_mask * self.q_hat)

            # entropy maximiser
            self.entropy_loss = tf.reduce_sum(self.q_hat * logits)


            self.loss = self.xe_loss + 0.01*self.suppression_loss + 0.01*self.entropy_loss


        self.q_hat_ids = tf.argmax(self.q_hat,axis=2,output_type=tf.int32)
        self.a_string = id_tensor_to_string(self.answer_coerced, self.rev_vocab, self.context_raw)
        self.q_hat_string = id_tensor_to_string(self.q_hat_ids, self.rev_vocab, self.context_raw)
        self.q_gold = id_tensor_to_string(self.question_coerced, self.rev_vocab, self.context_raw)
        self._output_summaries.extend(
            [tf.summary.text("q_hat", self.q_hat_string),
            tf.summary.text("q_gold", self.q_gold),
            # tf.summary.text("q_gold_ids", tf.as_string(self.question_ids)),
            # tf.summary.text("q_raw", self.question_raw),
            # tf.summary.text("context", self.context_raw),
            tf.summary.text("answer", self.answer_raw)])

        # Calculate and clip gradients
        params = tf.trainable_variables()
        gradients = tf.gradients(self.loss, params)
        clipped_gradients, _ = tf.clip_by_global_norm(
            gradients, 5)

        # Optimization
        self.optimizer = tf.train.AdamOptimizer(FLAGS.learning_rate).apply_gradients(
            zip(clipped_gradients, params)) if self.training_mode else tf.no_op()

        self.accuracy = tf.reduce_mean(tf.cast(tf.equal(self.question_ids,tf.argmax(self.q_hat,axis=2,output_type=tf.int32)),tf.float32)*self.target_weights)

    class OutputLayer(base.Layer):
        def __init__(self, model):
            super(model.OutputLayer, self).__init__(trainable=True,
                                        activity_regularizer=None)
            self.model = model

        def call(self, t,  prev_input, output, state, attention):
            with tf.variable_scope('decode_output', reuse=tf.AUTO_REUSE):
                print("t", t)
                print("y_t-1", prev_input)
                print("out_t", output)
                print("s_t", state)
                print("attn", attention)
                # switch takes st, vt and yt−1 as inputs
                # vt = concat(weighted context encoding at t; condition encoding)
                # st = hidden state at t
                # y_t-1 is previous generated token
                context = tf.matmul( attention, self.model.context_embedded[:,t,:])
                print("cntxt",context)
                # ha_tiled = tf.tile(tf.expand_dims(self.model.context_encoding,axis=1),[1,tf.reduce_max(self.model.question_length),1])
                vt = tf.concat([context, self.model.context_encoding], axis=1)
                # NOTE: outputs.rnn_output is y_t-1, should be prev state
                switch_input = tf.concat([vt, state, prev_input],axis=1)
                switch_h1 = tf.layers.dense(switch_input, 64, activation=tf.nn.tanh, kernel_initializer=tf.initializers.orthogonal())
                switch_h2 = tf.layers.dense(switch_h1, 64, activation=tf.nn.tanh, kernel_initializer=tf.initializers.orthogonal())
                switch = tf.layers.dense(switch_h2, 1, activation=tf.sigmoid, kernel_initializer=tf.initializers.orthogonal())

                # exit()

                # build shortlist prediction prob vector
                projection_layer = tf.layers.Dense(
                    len(self.model.vocab), use_bias=False)
                logits = projection_layer(output)
                q_hat_shortlist = tf.nn.softmax(logits,dim=1) #NOTE kwarg dim is deprecated in favour of axis, but blaze == 1.4

                # combine
                q_hat = tf.concat([(1-switch)*q_hat_shortlist,switch*attention], axis=1)

                print("qhat",q_hat)

                return ops.safe_log(q_hat)

        def build(self,input_shape):
            self.built=True

        def compute_output_shape(self, input_shape):
            # print(input_shape)
            # exit()
            input_shape = tensor_shape.TensorShape(input_shape)

            input_shape = input_shape.with_rank_at_least(2)
            if input_shape[-1].value is None:
              raise ValueError(
                  'The innermost dimension of input_shape must be defined, but saw: %s'
                  % input_shape)
            return input_shape[:-1].concatenate(1)
