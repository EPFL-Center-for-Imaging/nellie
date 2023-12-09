import os
import pandas as pd
import numpy as np
import skimage.measure
import matplotlib.pyplot as plt
from src_2.im_info.im_info import ImInfo
from sklearn.metrics import roc_curve, roc_auc_score
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.preprocessing import StandardScaler
import seaborn as sns
from scipy.stats import ttest_ind


top_dir = r"D:\test_files\nelly_multichannel"
# get all non-folder files
all_files = os.listdir(top_dir)
all_files = [os.path.join(top_dir, file) for file in all_files if not os.path.isdir(os.path.join(top_dir, file))]

label_types = ['organelle', 'branch']
# label_types = ['branch']
df_types = ['combo', 'motility', 'morphology']
# df_types = ['combo', 'morphology']

for label_type in label_types:
    for df_type_to_use in df_types:
        # generate a random guess line
        r_x, r_y = [0, 1], [0, 1]
        plt.figure(figsize=(10, 10))
        reports = []
        fprs = []
        tprs = []
        roc_aucs = []
        # for file_to_leave_out in range(2):
        for file_to_leave_out in range(len(all_files)):
            print(f'Leaving out file {file_to_leave_out + 1} of {len(all_files)}')
            test_file = all_files[file_to_leave_out]

            test_iminfo = ImInfo(test_file, ch=-1)
            test_motility_df = pd.read_csv(test_iminfo.pipeline_paths[f'{label_type}_motility_features'])
            test_motility_df.reset_index(drop=True, inplace=True)
            test_label_morphology_df = pd.read_csv(test_iminfo.pipeline_paths[f'{label_type}_label_features'])
            test_label_morphology_df.reset_index(drop=True, inplace=True)
            test_branch_morphology_df = pd.read_csv(test_iminfo.pipeline_paths[f'{label_type}_skeleton_features'])
            test_branch_morphology_df.reset_index(drop=True, inplace=True)

            test_combo_morphology_df = pd.concat([test_label_morphology_df, test_branch_morphology_df], axis=1)

            test_combo_df = pd.concat([test_combo_morphology_df, test_motility_df], axis=1)

            test_ch0_gt = ImInfo(test_file, ch=0)
            test_ch1_gt = ImInfo(test_file, ch=1)

            # get label image, frame 1 from multichannel file
            # get the label image from the multichannel file
            if label_type == 'organelle':
                pipeline_path = 'im_instance_label'
            elif label_type == 'branch':
                pipeline_path = 'im_skel_relabelled'
            else:
                raise ValueError(f'Invalid label_type: {label_type}')
            test_all_labels = test_iminfo.get_im_memmap(test_iminfo.pipeline_paths[pipeline_path])[1]
            # get the label mask from the single channel files
            test_ch0_gt_mask = test_ch0_gt.get_im_memmap(test_ch0_gt.pipeline_paths[pipeline_path])[1]>0
            test_ch1_gt_mask = test_ch1_gt.get_im_memmap(test_ch1_gt.pipeline_paths[pipeline_path])[1]>0

            gt_ch = []

            non_0_all_regions = skimage.measure.regionprops(test_all_labels)
            # if a branch has more overlap with mitochondria mask, its a mitochondria (ch0)
            # if a branch has more overlap with golgi mask, its a golgi (ch1)
            # this is our ground truth
            for region_num, region in enumerate(non_0_all_regions):
                ch0_sum_px = np.sum(test_ch0_gt_mask[region.coords[:, 0], region.coords[:, 1], region.coords[:, 2]])
                ch1_sum_px = np.sum(test_ch1_gt_mask[region.coords[:, 0], region.coords[:, 1], region.coords[:, 2]])
                if ch0_sum_px > ch1_sum_px:
                    gt_ch.append(0)
                else:
                    gt_ch.append(1)

            # add the groundtruth channel numbers to the dataframes for the test file
            test_combo_df['gt_ch'] = gt_ch
            test_motility_df['gt_ch'] = gt_ch
            test_combo_morphology_df['gt_ch'] = gt_ch

            if df_type_to_use == 'combo':
                test_df_to_use = test_combo_df.copy()
            elif df_type_to_use == 'motility':
                test_df_to_use = test_motility_df.copy()
            elif df_type_to_use == 'morphology':
                test_df_to_use = test_combo_morphology_df.copy()
            else:
                raise ValueError(f'Invalid df_type_to_use: {df_type_to_use}')

            test_df_to_use = test_df_to_use.replace([np.inf, -np.inf], np.nan)
            test_df_to_use = test_df_to_use.dropna()
            test_df_to_use.reset_index(drop=True, inplace=True)
            og_test_df = test_df_to_use.copy()

            train_motility_df = pd.DataFrame()
            train_label_morphology_df = pd.DataFrame()
            train_branch_morphology_df = pd.DataFrame()
            ch_array = []
            for ch in range(2):
                for train_file in all_files:
                    if train_file == test_file:
                        continue
                    train_iminfo = ImInfo(train_file, ch=ch)

                    new_motility_df = pd.read_csv(train_iminfo.pipeline_paths[f'{label_type}_motility_features'])
                    train_motility_df = pd.concat([train_motility_df, new_motility_df], axis=0)
                    train_motility_df.reset_index(drop=True, inplace=True)

                    new_label_morphology_df = pd.read_csv(train_iminfo.pipeline_paths[f'{label_type}_label_features'])
                    train_label_morphology_df = pd.concat([train_label_morphology_df, new_label_morphology_df], axis=0)
                    train_label_morphology_df.reset_index(drop=True, inplace=True)

                    new_branch_morphology_df = pd.read_csv(train_iminfo.pipeline_paths[f'{label_type}_skeleton_features'])
                    train_branch_morphology_df = pd.concat([train_branch_morphology_df, new_branch_morphology_df], axis=0)
                    train_branch_morphology_df.reset_index(drop=True, inplace=True)

                    # add the channel number to the ch_array, with length equal to the number of rows in the new dataframe
                    ch_array.extend(np.ones(new_motility_df.shape[0], dtype=int) * ch)
            train_combo_morphology_df = pd.concat([train_label_morphology_df, train_branch_morphology_df], axis=1)
            train_combo_df = pd.concat([train_combo_morphology_df, train_motility_df], axis=1)


            if df_type_to_use == 'combo':
                train_df_to_use = train_combo_df.copy()
            elif df_type_to_use == 'motility':
                train_df_to_use = train_motility_df.copy()
            elif df_type_to_use == 'morphology':
                train_df_to_use = train_combo_morphology_df.copy()
            else:
                raise ValueError(f'Invalid df_type_to_use: {df_type_to_use}')

            train_df_to_use['gt_ch'] = ch_array

            # convert infinities to nan
            train_df_to_use = train_df_to_use.replace([np.inf, -np.inf], np.nan)
            train_df_to_use = train_df_to_use.dropna()
            train_df_to_use.reset_index(drop=True, inplace=True)

            og_train_df = train_df_to_use.copy()

            remove_cols = [
                'main_label', 'file', 'label', 't', 'gt_ch',
                'intensity_mean', 'intensity_std', 'intensity_range',
                'frangi_mean', 'frangi_std', 'frangi_range',
            ]

            stats_to_use = ['median', 'max', 'std', 'min']
            features_to_use = ['rel_ang_vel_mag_12', 'rel_ang_acc_mag',
                               'rel_lin_vel_mag_12', 'rel_lin_acc_mag',
                               'ref_lin_vel_mag_12', 'ref_lin_acc_mag',
                               'com_directionality_12', 'com_directionality_acceleration']
            keep_motility_cols = []
            for feature in features_to_use:
                for stat in stats_to_use:
                    keep_motility_cols.append(f'{feature}_{stat}')
            remove_motility_cols = [col for col in train_motility_df.columns if col not in keep_motility_cols]
            remove_cols.extend(remove_motility_cols)
            for col in remove_cols:
                if col in test_df_to_use.columns:
                    test_df_to_use.drop(columns=col, inplace=True)
                if col in train_df_to_use.columns:
                    train_df_to_use.drop(columns=col, inplace=True)

            # remove any column with "_01_" in the name
            for col in test_df_to_use.columns:
                if '_01_' in col:
                    test_df_to_use.drop(columns=col, inplace=True)
            for col in train_df_to_use.columns:
                if '_01_' in col:
                    train_df_to_use.drop(columns=col, inplace=True)

            # assert all columns are the same
            assert all(test_df_to_use.columns == train_df_to_use.columns)

            # train a model on all but test file with combined branch data from all files
            # train a morphology only model, a motility only model, and a combined model

            # # Volcano plot stuff
            # group1 = test_df_to_use[og_test_df['gt_ch'] == 0]
            # group2 = test_df_to_use[og_test_df['gt_ch'] == 1]
            #
            # # Compute fold change and p-values
            # features = test_df_to_use.columns.difference(['gt_ch'])  # Adjust as needed
            # fold_changes = group1[features].mean() / group2[features].mean()
            # p_values = ttest_ind(group1[features], group2[features], axis=0).pvalue
            #
            # # Prepare DataFrame for volcano plot
            # volcano_df = pd.DataFrame({
            #     'Feature': features,
            #     'Log2FoldChange': np.log2(fold_changes),
            #     '-Log10PValue': -np.log10(p_values)
            # })
            #
            # # Plotting the Volcano Plot with Annotations
            # plt.figure(figsize=(12, 8))
            # plt.title('Volcano Plot')
            # plt.xlabel('Log2 Fold Change')
            # plt.ylabel('-Log10 P-Value')
            # sns.scatterplot(data=volcano_df, x='Log2FoldChange', y='-Log10PValue', edgecolor=None)
            #
            # # Annotating significant features
            # # Define your criteria for significance
            # significant = volcano_df[volcano_df['-Log10PValue'] > -np.log10(0.05)]
            # for index, row in significant.iterrows():
            #     plt.text(row['Log2FoldChange'], row['-Log10PValue'], row['Feature'], horizontalalignment='left',
            #              size='small', color='black', weight='semibold')
            #
            # sns.scatterplot(data=significant, x='Log2FoldChange', y='-Log10PValue', color='red', edgecolor=None)
            # plt.savefig(os.path.join(top_dir, 'output', f'volcano_plot-{label_type}-{df_type_to_use}-{file_to_leave_out}.png'), dpi=500)
            # plt.close()

            #shuffle the training dataset and og dataset in the same way
            train_df_to_use = train_df_to_use.sample(frac=1, random_state=42)
            og_train_df = og_train_df.iloc[train_df_to_use.index]

            scaler = StandardScaler()
            train_standardized_features = scaler.fit_transform(train_df_to_use)
            train_standardized_features = pd.DataFrame(train_standardized_features, columns=train_df_to_use.columns)
            test_standardized_features = scaler.transform(test_df_to_use)
            test_standardized_features = pd.DataFrame(test_standardized_features, columns=test_df_to_use.columns)

            # test the model on the test file, have it generate a vector assigning branches to the golgi channel or mito channel (0 or 1)
            clf = RandomForestClassifier(n_estimators=300, random_state=42, n_jobs=-1)
            clf.fit(train_standardized_features, og_train_df['gt_ch'])
            y_pred = clf.predict(test_standardized_features)

            # Evaluate the model with cross-validation
            # compare the vector to the ground truth for metrics
            report = classification_report(og_test_df['gt_ch'], y_pred, output_dict=True)

            # get roc curve
            # get the probabilities for each class
            y_prob = clf.predict_proba(test_standardized_features)
            # get the probabilities for the positive class
            y_prob = y_prob[:, 1]
            # calculate roc curve
            fpr, tpr, thresholds = roc_curve(og_test_df['gt_ch'], y_prob)
            # calculate roc auc
            roc_auc = roc_auc_score(og_test_df['gt_ch'], y_prob)
            print(f'ROC AUC: {roc_auc}\n')

            reports.append(report)
            fprs.append(fpr)
            tprs.append(tpr)
            roc_aucs.append(roc_auc)

            plt.plot(fpr, tpr, marker='.')
        plt.plot(r_x, r_y, linestyle='--')
        plt.title('ROC Curve')
        plt.xlabel('False Positive Rate')
        plt.ylabel('True Positive Rate')
        # plt.show()
        plt.savefig(os.path.join(top_dir, 'output', f'roc_curves-{label_type}-{df_type_to_use}.png'), dpi=500)
        plt.close()
        print(f'ROC AUC: {roc_auc}')

        # save all the reports metrics to a csv
        prec_0 = []
        prec_1 = []
        rec_0 = []
        rec_1 = []
        f1_0 = []
        f1_1 = []
        acc = []
        macro_avg = []
        weighted_avg = []
        for report in reports:
            prec_0.append(report['0']['precision'])
            prec_1.append(report['1']['precision'])
            rec_0.append(report['0']['recall'])
            rec_1.append(report['1']['recall'])
            f1_0.append(report['0']['f1-score'])
            f1_1.append(report['1']['f1-score'])
            acc.append(report['accuracy'])
            macro_avg.append(report['macro avg']['f1-score'])
            weighted_avg.append(report['weighted avg']['f1-score'])

        report_df = pd.DataFrame({
            'prec_0': prec_0,
            'rec_0': rec_0,
            'f1_0': f1_0,
            'prec_1': prec_1,
            'rec_1': rec_1,
            'f1_1': f1_1,
            'acc': acc,
            'macro_avg': macro_avg,
            'weighted_avg': weighted_avg,
            'roc_auc': roc_aucs,
        })
        report_df.to_csv(os.path.join(top_dir, 'output', f'reports-{label_type}-{df_type_to_use}.csv'), index=False)
